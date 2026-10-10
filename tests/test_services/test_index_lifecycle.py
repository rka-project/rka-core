"""Index rows must die with their entities, and be visible when they do not.

`_DELETE_TABLES` lists 72 project-scoped tables and not one `vec_*` or
`fts_*`. Those tables carry no `project_id`, so the scoped loop could not
reach them and deleting a project simply left them behind: 774 vector rows
and 2914 FTS rows on this instance, from projects removed months ago.

They never appeared in results — hydration filters them out — but they were
filtered *after* taking a slot in the ranked window, so a live entry ranked
below them was never fetched. `check_integrity` reported none of it, and also
missed 35 journal entries whose project row is gone: rows that exist in the
database and no API path can read.
"""

import inspect

import pytest

from rka.infra.database import Database
from rka.services.knowledge_pack import KnowledgePackService
from rka.services.project import ProjectService


async def _project(db: Database, pid: str) -> None:
    await db.execute(
        "INSERT OR IGNORE INTO projects (id, name, description, created_by) "
        "VALUES (?, ?, ?, ?)",
        [pid, pid, pid, "system"],
    )
    await db.commit()


async def _entry(db: Database, pid: str, eid: str) -> None:
    await db.execute(
        "INSERT INTO journal (id, project_id, type, content, source, confidence, "
        "importance, status) VALUES (?, ?, 'note', 'content', 'executor', "
        "'hypothesis', 'normal', 'active')",
        [eid, pid],
    )
    await db.execute(
        "INSERT INTO fts_journal (id, content, summary) VALUES (?, 'content', '')",
        [eid],
    )
    await db.commit()


class TestDeletionTakesTheIndexWithIt:
    @pytest.mark.asyncio
    async def test_deleting_a_project_removes_its_fts_rows(self, db: Database):
        await _project(db, "prj_doomed")
        await _entry(db, "prj_doomed", "jrn_doomed")

        svc = ProjectService(db)
        await svc.delete_project("prj_doomed", confirm=True)

        left = await db.fetchall(
            "SELECT id FROM fts_journal WHERE id = ?", ["jrn_doomed"],
        )
        assert not left, (
            "the index row outlived its entity and goes on competing for "
            "slots in every search"
        )

    @pytest.mark.asyncio
    async def test_another_projects_index_rows_survive(self, db: Database):
        """The subquery must be scoped, or deletion becomes destructive."""
        await _project(db, "prj_doomed")
        await _project(db, "prj_keep")
        await _entry(db, "prj_doomed", "jrn_doomed")
        await _entry(db, "prj_keep", "jrn_keep")

        await ProjectService(db).delete_project("prj_doomed", confirm=True)

        kept = await db.fetchall("SELECT id FROM fts_journal WHERE id = ?", ["jrn_keep"])
        assert kept, "deleting one project destroyed another's index"

    def test_index_cleanup_runs_before_the_source_delete(self):
        """Order is the whole correctness argument.

        The index tables have no project_id; they can only be identified by
        joining the source rows, so once those are gone nothing identifies
        them.
        """
        src = inspect.getsource(ProjectService._delete_project)
        assert src.index("_INDEX_TABLES") < src.index("for table in self._DELETE_TABLES")

    def test_every_indexed_entity_is_covered(self):
        """A new indexed entity must join the map, or leak on delete."""
        from rka.services.base import BaseService

        fts_sources = {
            "journal": "journal", "decision": "decisions",
            "literature": "literature", "mission": "missions",
            "claim": "claims", "cluster": "evidence_clusters",
        }
        covered = {src for src, _ in ProjectService._INDEX_TABLES}
        for etype in BaseService._FTS_CONFIG:
            assert fts_sources[etype] in covered, (
                f"{etype} has an FTS index but its source table is not in "
                "_INDEX_TABLES; deleting a project would leak its rows"
            )


class TestIntegrityCanSeeThem:
    @pytest.mark.asyncio
    async def test_an_orphaned_fts_row_is_reported(self, db: Database):
        await _project(db, "prj_x")
        await db.execute(
            "INSERT INTO fts_journal (id, content, summary) VALUES (?, 'x', '')",
            ["jrn_ghost"],
        )
        await db.commit()

        issues = await KnowledgePackService(db, project_id="prj_x").check_integrity()
        cats = {i["category"] for i in issues}
        assert "orphaned_fts_rows" in cats

    @pytest.mark.asyncio
    async def test_a_stranded_entity_is_reported(self, db: Database):
        await _project(db, "prj_x")
        await db.execute(
            "INSERT INTO journal (id, project_id, type, content, source, "
            "confidence, importance, status) VALUES (?, 'prj_vanished', 'note', "
            "'x', 'executor', 'hypothesis', 'normal', 'active')",
            ["jrn_stranded"],
        )
        await db.commit()

        issues = await KnowledgePackService(db, project_id="prj_x").check_integrity()
        found = [i for i in issues if i["category"] == "stranded_entities"]
        assert found, "35 of these exist live and nothing reported them"
        assert "jrn_stranded" in found[0]["ids"]
        assert found[0]["scope"] == "database"
        assert "project_id" not in found[0]
        assert found[0]["count_is_exact"] is False
        # This diagnostic is not recovery or permission to purge content.
        row = await db.fetchone("SELECT content, project_id FROM journal WHERE id = 'jrn_stranded'")
        assert row == {"content": "x", "project_id": "prj_vanished"}

    @pytest.mark.asyncio
    async def test_a_clean_database_reports_neither(self, db: Database):
        await _project(db, "prj_x")
        await _entry(db, "prj_x", "jrn_ok")

        issues = await KnowledgePackService(db, project_id="prj_x").check_integrity()
        cats = {i["category"] for i in issues}
        assert "orphaned_fts_rows" not in cats
        assert "stranded_entities" not in cats

    def test_the_vector_check_does_not_depend_on_the_extension(self):
        """It reads the `_rowids` shadow, which is a plain table.

        Querying `vec_*` directly raises "no such module: vec0" wherever
        sqlite-vec is not loaded, and the first version of this check
        swallowed that and reported zero orphans — a clean bill of health
        from a check that never looked.
        """
        src = inspect.getsource(KnowledgePackService._index_integrity_issues)
        assert "_rowids" in src
        assert "index_check_incomplete" in src, (
            "a check that cannot read a table must say so, not imply it is clean"
        )
