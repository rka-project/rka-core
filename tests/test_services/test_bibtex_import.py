"""I1 regressions: real parser, origin/actor separation, and transaction read-back."""

import sys

import pytest
import pytest_asyncio

from rka.infra.file_access import FileAccessPolicy
from rka.models.literature import LiteratureCreate
from rka.services.academic import AcademicImportService
from rka.services.literature import LiteratureService

BIBTEX = """@article{nested,
  title = {A {Nested {AI}} Title},
  author = {Doe, Jane and Smith, John},
  year = 2026,
  journal = "Synthetic Journal",
  doi = {10.0000/synthetic},
  abstract = {A result with {nested} evidence.}
}"""


@pytest_asyncio.fixture(params=["academic", "base"])
async def importer(request, db, tmp_path, monkeypatch):
    if request.param == "base":
        monkeypatch.setitem(sys.modules, "bibtexparser", None)
    else:
        pytest.importorskip("bibtexparser")
    return AcademicImportService(LiteratureService(db), file_policy=FileAccessPolicy([tmp_path]))


@pytest.mark.asyncio
async def test_bibtex_preserves_nested_fields_raw_source_and_import_origin(importer, db):
    result = await importer.import_bibtex(BIBTEX, default_status="read")
    assert not result["errors"], result
    assert len(result["imported"]) == result["total_parsed"] == 1
    stored = await importer.lit.get(result["imported"][0]["id"])
    assert stored.title == "A Nested AI Title"
    assert stored.authors == ["Doe, Jane", "Smith, John"]
    assert stored.year == 2026
    assert stored.venue == "Synthetic Journal"
    assert stored.abstract == "A result with nested evidence."
    assert stored.status == "read"
    assert stored.added_by == "import"
    assert stored.bibtex == BIBTEX
    event = await db.fetchone("SELECT actor FROM events WHERE entity_id = ?", [stored.id])
    assert event["actor"] == "system"


@pytest.mark.asyncio
@pytest.mark.parametrize("via_file", [False, True])
async def test_utf8_bom_does_not_hide_the_first_entry(importer, tmp_path, via_file):
    text = "\ufeff" + BIBTEX
    if via_file:
        path = tmp_path / "bom-refs.bib"
        path.write_text(text, encoding="utf-8")
        result = await importer.import_bibtex_file(str(path))
    else:
        result = await importer.import_bibtex(text)
    assert not result["errors"], result
    assert len(result["imported"]) == result["total_parsed"] == 1, result
    stored = await importer.lit.get(result["imported"][0]["id"])
    assert stored.title == "A Nested AI Title"
    assert stored.bibtex == BIBTEX


@pytest.mark.asyncio
async def test_bibtex_file_and_single_line_duplicates(importer, tmp_path):
    path = tmp_path / "refs.bib"
    path.write_text(BIBTEX, encoding="utf-8")
    first = await importer.import_bibtex_file(str(path))
    assert len(first["imported"]) == 1, first
    duplicate = await importer.import_bibtex(
        "@article{other, title={Other title}, doi={10.0000/synthetic}}"
    )
    assert len(duplicate["skipped"]) == 1, duplicate
    assert not duplicate["errors"]
    repeated = await importer.import_bibtex(BIBTEX, skip_duplicates=False)
    # Opting out of the preflight skip does not bypass the existing unique DOI
    # constraint: report that one entry failed, without a partial record.
    assert len(repeated["errors"]) == 1, repeated
    assert not repeated["imported"] and not repeated["skipped"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "bad", ["@article{bad, title={Unclosed", "@article{bad, title={One}, title={Two}}"]
)
async def test_malformed_bibtex_reports_error_without_partial_writes(importer, db, bad):
    result = await importer.import_bibtex(BIBTEX + "\n" + bad)
    assert result["errors"], result
    assert not result["imported"]
    assert (await db.fetchone("SELECT count(*) AS n FROM literature"))["n"] == 0


@pytest.mark.asyncio
async def test_per_entry_failure_rolls_back_and_valid_entries_continue(importer, db, monkeypatch):
    original_emit = importer.lit.emit_event

    async def emit(*args, **kwargs):
        if "Fail this entry" in kwargs.get("summary", ""):
            raise RuntimeError("synthetic event failure")
        return await original_emit(*args, **kwargs)

    monkeypatch.setattr(importer.lit, "emit_event", emit)
    result = await importer.import_bibtex("@article{failed, title={Fail this entry}}\n" + BIBTEX)
    assert len(result["errors"]) == len(result["imported"]) == 1, result
    assert result["total_parsed"] == 2
    assert [row["title"] for row in await db.fetchall("SELECT title FROM literature")] == [
        "A Nested AI Title"
    ]
    assert len(await db.fetchall("SELECT * FROM events WHERE event_type='literature_added'")) == 1


@pytest.mark.asyncio
async def test_literature_import_origin_is_not_an_actor_alias(db):
    service = LiteratureService(db)
    record = await service.create(LiteratureCreate(title="Direct import", added_by="import"))
    assert record.added_by == "import"
    assert (await db.fetchone("SELECT actor FROM events WHERE entity_id = ?", [record.id]))[
        "actor"
    ] == "system"
    with pytest.raises(ValueError, match="Invalid actor"):
        await service.create(LiteratureCreate(title="Explicit invalid actor"), actor="import")
    assert (await db.fetchone("SELECT count(*) AS n FROM literature"))["n"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("actor", ["system", "executor", "brain", "pi", "web_ui", "llm"])
async def test_note_import_actor_preserves_source_and_verbatim(db, actor):
    from rka.models.journal import JournalEntryCreate
    from rka.services.notes import NoteService

    original = "Synthetic original PI wording"
    note = await NoteService(db).create(
        JournalEntryCreate(content=original, source="pi", verbatim_input=original),
        actor=actor,
    )
    assert note.source == "pi"
    assert note.verbatim_input == original
    assert (await db.fetchone("SELECT actor FROM events WHERE entity_id = ?", [note.id]))[
        "actor"
    ] == actor
    assert (await db.fetchone("SELECT actor FROM audit_log WHERE entity_id = ?", [note.id]))[
        "actor"
    ] == actor
