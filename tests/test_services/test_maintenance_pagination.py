"""Audit regressions: counts are not page lengths or unique entity counts."""

import pytest

from rka.infra.database import Database
from rka.services.lifecycle import DirectiveDependencyService
from rka.services.maintenance import MaintenanceService


async def _seed(db, project_id, size=84):
    await db.execute("INSERT INTO projects (id, name) VALUES (?, ?)", [project_id, project_id])
    for i in range(size):
        note_id = f"jrn_{project_id}_{i:03}"
        await db.execute(
            "INSERT INTO journal (id, project_id, type, content, source) VALUES (?, ?, ?, ?, 'executor')",
            [note_id, project_id, "note" if i < 59 else "log", "Synthetic audit fixture"],
        )
        if i >= 11:
            await db.execute(
                "INSERT INTO tags (entity_type, entity_id, tag, project_id) VALUES ('journal', ?, 'test', ?)",
                [note_id, project_id],
            )


async def test_totals_match_summary_above_list_cap_and_pages_cover_all_ids(db):
    await _seed(db, "prj_audit")
    await _seed(db, "prj_other", 20)
    svc = MaintenanceService(db, project_id="prj_audit")
    summary = await svc.get_backlog_summary()
    first = await svc.get_pending_maintenance()
    second = await svc.get_pending_maintenance(offset=50)
    end = await svc.get_pending_maintenance(offset=200)
    assert summary["total_items"] == first["total_items"] == second["total_items"] == end["total_items"] == 154
    assert first["returned_count"] == 111  # 50 crossrefs + 50 claims + 11 tags
    assert second["returned_count"] == 43
    assert end["returned_count"] == 0
    assert end["has_more"] is False
    assert first["scope"] == "project"
    assert first["project_id"] == "prj_audit"
    assert first["count_unit"] == "issue_occurrences"
    for name, total in (("entries_missing_cross_refs", 84), ("entries_without_claims", 59), ("entries_without_tags", 11)):
        a, b = first["categories"][name], second["categories"][name]
        assert a["count"] == a["category_total"] == b["count"] == total
        assert a["returned_count"] == len(a["ids"])
        assert a["next_offset"] == (50 if total > 50 else None)
        assert not set(a["ids"]) & set(b["ids"])
        assert len(set(a["ids"] + b["ids"])) == total
        assert all("prj_audit" in row_id for row_id in a["ids"] + b["ids"])
    # Estimates refer to the same total scope, not a changing page length.
    assert first["estimated_tool_calls"] == second["estimated_tool_calls"] == 213


async def test_dependency_pairs_are_not_collapsed_to_unique_directives(db, monkeypatch):
    async def candidates(self):
        return {
            "count": 3, "ids": ["jrn_shared"],
            "candidates": [{"id": "jrn_shared", "decision_id": f"dec_{i}"} for i in range(3)],
            "description": "Synthetic dependencies", "fix_action": "Review", "fix_calls_per_item": 1,
        }
    monkeypatch.setattr(DirectiveDependencyService, "review_candidates", candidates)
    svc = MaintenanceService(db)
    first = await svc.get_pending_maintenance(limit=2)
    second = await svc.get_pending_maintenance(limit=2, offset=2)
    assert first["total_items"] == second["total_items"] == (await svc.get_backlog_summary())["total_items"] == 3
    category = first["categories"]["directive_dependency_gaps"]
    assert category["count"] == 3
    assert category["returned_count"] == 2
    assert len(category["ids"]) == 1
    assert category["next_offset"] == 2
    assert category["advisory_limit"] == 100
    assert second["categories"]["directive_dependency_gaps"]["candidates"] == [{"id": "jrn_shared", "decision_id": "dec_2"}]


@pytest.mark.parametrize("kwargs", [{"limit": 0}, {"limit": 201}, {"offset": -1}])
async def test_service_rejects_invalid_page(db, kwargs):
    with pytest.raises(ValueError):
        await MaintenanceService(db).get_pending_maintenance(**kwargs)


async def test_empty_project_retains_zero_total(db):
    manifest = await MaintenanceService(db).get_pending_maintenance()
    assert manifest["total_items"] == manifest["returned_count"] == 0
    assert manifest["has_more"] is False


async def test_page_and_counts_use_one_read_snapshot(db, monkeypatch):
    await _seed(db, "prj_snapshot", 2)
    writer = Database(db.db_path)
    await writer.connect()
    svc = MaintenanceService(db, project_id="prj_snapshot")
    original = svc._entries_without_tags

    async def change_after_first_page(pid, **page):
        rows = await original(pid, **page)
        await writer.execute(
            "INSERT INTO journal (id, project_id, type, content, source) VALUES ('jrn_concurrent', ?, 'note', 'Concurrent fixture', 'executor')",
            [pid],
        )
        return rows

    monkeypatch.setattr(svc, "_entries_without_tags", change_after_first_page)
    try:
        manifest = await svc.get_pending_maintenance()
        assert manifest["total_items"] == manifest["returned_count"] == 6
        assert all("jrn_concurrent" not in c["ids"] for c in manifest["categories"].values())
        # The committed concurrent insert is visible on the next request.
        assert (await svc.get_backlog_summary())["total_items"] == 9
    finally:
        await writer.close()
