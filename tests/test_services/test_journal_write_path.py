"""What a journal entry is, and who wrote it.

Five defects in one write path, each of which corrupted or hid the record
rather than failing:

  A1  a partial update rewrote `source`, `confidence` and `importance` to the
      dispatcher's create-time defaults, so a PI directive became an executor
      note and a `verified` finding reverted to `hypothesis` — while
      `verbatim_input` survived, leaving the row asserting that the Executor
      had written the PI's exact words.
  A5  superseding set `confidence` but not `status`, and `hide_superseded`
      filters on `status`. 26 superseded entries were live; 0 were filterable.
  A6  `summary` was written to FTS as the empty string at create time.
  B5  REST defaulted `source` to 'pi' and never checked `verbatim_input`,
      so omitting a field claimed PI authorship. 46 live entries claim it
      with no verbatim record, 22 in real research projects.
  D3  `record_note` could write `summary`; `update_note` could not correct it.
"""

import inspect

import pytest

from rka.infra.database import Database

from rka.mcp import verb_dispatch
from rka.mcp.operation_args import RecordNoteArgs, UpdateNoteArgs
from rka.models.journal import JournalEntryCreate, JournalEntryUpdate


class TestAnUpdateDoesNotFabricateIdentity:
    """A1 — the one that corrupted data at rest."""

    @pytest.mark.asyncio
    async def test_update_note_sends_only_what_the_caller_set(self, monkeypatch):
        captured: dict = {}

        async def _fake_review(op, *, project_id, payload, **kw):
            captured["op"] = op
            captured["payload"] = payload
            return "{}"

        monkeypatch.setattr(verb_dispatch, "dispatch_review", _fake_review)
        await verb_dispatch.dispatch_execute(
            "update_note", project_id="prj_x", id="jrn_x", content="revised",
        )

        payload = captured["payload"]
        for field in ("source", "confidence", "importance"):
            assert field not in payload or payload[field] is None, (
                f"update_note fabricated {field}={payload.get(field)!r}; the "
                "caller never mentioned it, and writing it rewrites the row"
            )
        assert payload.get("content") == "revised"

    @pytest.mark.asyncio
    async def test_an_explicit_value_still_reaches_the_payload(self, monkeypatch):
        """Skipping the defaults must not skip what the caller asked for."""
        captured: dict = {}

        async def _fake_review(op, *, project_id, payload, **kw):
            captured.update(payload)
            return "{}"

        monkeypatch.setattr(verb_dispatch, "dispatch_review", _fake_review)
        await verb_dispatch.dispatch_execute(
            "update_note", project_id="prj_x", id="jrn_x",
            confidence="verified", source="pi", verbatim_input="exact words",
        )
        assert captured["confidence"] == "verified"
        assert captured["source"] == "pi"

    @pytest.mark.asyncio
    async def test_creates_still_get_their_defaults(self, monkeypatch):
        """The defaults exist for a reason; only updates must skip them."""
        captured: dict = {}

        async def _fake_review(op, *, project_id, payload, **kw):
            captured.update(payload)
            return "{}"

        monkeypatch.setattr(verb_dispatch, "dispatch_review", _fake_review)
        await verb_dispatch.dispatch_execute(
            "create_cluster", project_id="prj_x", label="c",
        )
        assert captured.get("source") == "executor"

    def test_signatures_preserve_omission_and_creation_defaults_match_the_model(self):
        """Guard both layers against injecting creation defaults into updates.

        I2 replaces value comparison with presence tracking: the wrappers must
        retain None until dispatch chooses whether a creation default applies.
        The defaults themselves must still agree with the journal create model.
        """
        from rka.mcp.server import _rka_execute_legacy_impl

        for field, recorded in verb_dispatch._IDENTITY_FIELD_DEFAULTS.items():
            assert JournalEntryCreate.model_fields[field].default == recorded
            for dispatcher in (verb_dispatch.dispatch_execute, _rka_execute_legacy_impl):
                assert inspect.signature(dispatcher).parameters[field].default is None, (
                    f"{dispatcher.__name__} must not fabricate {field} on an update"
                )

    @pytest.mark.asyncio
    @pytest.mark.parametrize("operation", sorted(verb_dispatch._IDENTITY_SENSITIVE_UPDATES))
    async def test_each_update_distinguishes_omission_from_explicit_defaults(
        self, monkeypatch, operation,
    ):
        captured = {}

        async def _fake_review(op, *, project_id, payload, **kw):
            captured.clear()
            captured.update(payload)
            return "{}"

        monkeypatch.setattr(verb_dispatch, "dispatch_review", _fake_review)
        await verb_dispatch.dispatch_execute(operation, project_id="prj_x", id="jrn_x")
        assert not (captured.keys() & verb_dispatch._IDENTITY_FIELD_DEFAULTS.keys())

        await verb_dispatch.dispatch_execute(
            operation, project_id="prj_x", id="jrn_x",
            **verb_dispatch._IDENTITY_FIELD_DEFAULTS,
        )
        for field, default in verb_dispatch._IDENTITY_FIELD_DEFAULTS.items():
            assert captured[field] == default

    def test_every_mutating_op_in_the_map_is_covered(self):
        """A new update op added to _REVIEW_OP_MAP must join the skip set.

        Scoped to that map specifically: dispatch_execute contains other
        op->action dicts, and `update_mission` lives in one of them and takes
        a different path entirely.
        """
        import re

        src = inspect.getsource(verb_dispatch.dispatch_execute)
        block = src.split("_REVIEW_OP_MAP = {")[1].split("}")[0]
        mapped = set(re.findall(r'"(\w+)":', block))
        mutating = {o for o in mapped if o.startswith("update_") or o == "bulk_update"}
        assert mutating, "guard against the extraction silently matching nothing"
        assert mutating <= set(verb_dispatch._IDENTITY_SENSITIVE_UPDATES), (
            f"{sorted(mutating - set(verb_dispatch._IDENTITY_SENSITIVE_UPDATES))} "
            "mutate an existing row but would still receive fabricated defaults"
        )


class TestSupersedingHides:
    """A5."""

    def test_supersede_sets_the_field_the_filter_reads(self):
        from rka.services import notes

        create_src = inspect.getsource(notes.NoteService.create)
        stmt = create_src.split("superseded_by = ?")[1][:200]
        assert "status = 'superseded'" in stmt, (
            "hide_superseded filters on status; setting only confidence "
            "meant the flag never hid anything"
        )

    def test_the_filter_still_reads_status(self):
        """If the filter moves, the fix above is aimed at the wrong column."""
        from rka.services import notes

        assert "status != 'superseded'" in inspect.getsource(notes.NoteService.list)


class TestTheSummaryIsSearchable:
    """A6 + D3 — together, a summary was unsearchable and uncorrectable."""

    def test_create_indexes_the_summary_it_was_given(self):
        from rka.services import notes

        src = inspect.getsource(notes.NoteService.create)
        assert '"summary": data.summary or ""' in src
        assert '{"content": data.content, "summary": ""}' not in src

    def test_update_note_can_correct_a_summary(self):
        assert "summary" in UpdateNoteArgs.model_fields, (
            "record_note could write summary and update_note could not fix it"
        )

    def test_update_note_accepts_summary_only(self):
        args = UpdateNoteArgs(
            operation="update_note",
            project_id="prj_x",
            id="jrn_x",
            summary="A corrected summary.",
        )
        assert args.summary == "A corrected summary."

    def test_the_advertised_schema_agrees(self):
        from rka.mcp.operations_schema import OPERATIONS_SCHEMA

        entry = OPERATIONS_SCHEMA["update_note"]
        assert "summary" in entry["optional_fields"]
        assert "summary=None" in entry["signature"]


class TestPiAuthorshipIsClaimedNotAssumed:
    """B5."""

    def test_omitting_source_does_not_claim_pi(self):
        assert JournalEntryCreate(content="x").source == "executor"

    def test_the_two_surfaces_now_agree(self):
        assert (
            JournalEntryCreate.model_fields["source"].default
            == RecordNoteArgs.model_fields["source"].default
        )

    def test_an_explicit_pi_claim_is_still_allowed(self):
        """The rule lives on the MCP surface, not here.

        `verbatim_input` guards restatement — an agent putting the PI's
        meaning in its own words — and RecordNoteArgs enforces it there.
        This model also serves workspace ingest, where `content` IS the PI's
        document and there is nothing to restate, so enforcing at this layer
        would reject a legitimate claim it cannot distinguish from a false
        one. Only the silent default was the defect.
        """
        assert JournalEntryCreate(content="x", source="pi").source == "pi"

    def test_the_mcp_surface_still_enforces_it(self):
        from rka.mcp.operation_args import RecordNoteArgs

        with pytest.raises(ValueError, match="verbatim_input"):
            RecordNoteArgs(operation="record_note", project_id="prj_x",
                           content="x", source="pi")

    def test_a_partial_update_that_omits_source_is_untouched(self):
        assert JournalEntryUpdate(content="y").source is None

    def test_workspace_ingest_no_longer_defaults_to_pi_either(self):
        """The same silent claim existed in the ingest request models."""
        from rka.models.workspace import WorkspaceIngestRequest

        assert WorkspaceIngestRequest.model_fields["source"].default == "executor"


async def _ensure_project(db, project_id: str) -> None:
    await db.execute(
        "INSERT OR IGNORE INTO projects (id, name, description, created_by) "
        "VALUES (?, ?, ?, ?)",
        [project_id, "T", "T", "system"],
    )


class TestTheRoundTrip:
    """End to end, against a real DB: the corruption A1 caused."""

    @pytest.mark.asyncio
    async def test_a_content_edit_does_not_demote_a_pi_directive(self, db: Database):
        from rka.services.notes import NoteService

        await _ensure_project(db, "prj_rt")
        svc = NoteService(db, llm=None, embeddings=None, project_id="prj_rt")

        entry = await svc.create(
            JournalEntryCreate(
                content="Paraphrase of what the PI asked for.",
                type="directive",
                source="pi",
                verbatim_input="Do it this way, exactly.",
                confidence="verified",
                importance="critical",
            ),
        )

        # Take the payload dispatch_execute actually builds and apply it.
        # Calling svc.update directly would pass on main too — the service
        # was never the problem; the fabrication happened one layer up, and a
        # round-trip that skips that layer proves nothing.
        captured: dict = {}

        async def _capture(op, *, project_id, payload, **kw):
            captured.update(payload)
            return "{}"

        import pytest as _pytest  # noqa: F401
        from _pytest.monkeypatch import MonkeyPatch

        mp = MonkeyPatch()
        try:
            mp.setattr(verb_dispatch, "dispatch_review", _capture)
            await verb_dispatch.dispatch_execute(
                "update_note", project_id="prj_rt", id=entry.id,
                content="Revised paraphrase.",
            )
        finally:
            mp.undo()

        captured.pop("id", None)
        await svc.update(entry.id, JournalEntryUpdate(**captured))

        after = await svc.get(entry.id)
        assert after.content == "Revised paraphrase."
        assert after.source == "pi", (
            "the PI directive became an executor note on a content edit"
        )
        assert after.confidence == "verified", "a verified finding reverted"
        assert after.importance == "critical"
        assert after.verbatim_input == "Do it this way, exactly.", (
            "verbatim survived the demotion, which is what made the row assert "
            "that the Executor had written the PI's exact words"
        )

    @pytest.mark.asyncio
    async def test_superseding_hides_the_old_entry(self, db: Database):
        from rka.services.notes import NoteService

        await _ensure_project(db, "prj_sup")
        svc = NoteService(db, llm=None, embeddings=None, project_id="prj_sup")

        old = await svc.create(JournalEntryCreate(content="First finding."))
        await svc.create(
            JournalEntryCreate(content="Corrected.", supersedes=old.id),
        )

        hidden = {e.id for e in await svc.list(hide_superseded=True)}
        assert old.id not in hidden, "hide_superseded did not hide it"

        shown = {e.id for e in await svc.list(hide_superseded=False)}
        assert old.id in shown, "it must still be reachable when not hidden"
