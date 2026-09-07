"""A failing language model must not look like a successful no-op.

Two of the changed paths returned HTTP 200 with nothing to separate "there is
no LLM" from "the LLM was configured, unreachable, and every call failed": the
workspace scan and the bootstrap review. `/api/summarize` returned a bare 503
that skipped the handler adding `error` and `hint`, and an MCP dispatch timeout
surfaced as a tool error with no message at all.

The scan case also repeated the failed call once per file, serially, with
`max_files` defaulting to 5000.
"""

import json
import logging
import types

import httpx
import pytest

from rka.models.workspace import ScanCapabilities
from rka.services.workspace import (
    _LLM_FAILURE_THRESHOLD,
    WorkspaceService,
    _LlmScanHealth,
)


class _NoDuplicates:
    """scan() checks each file's hash against bootstrap_log."""

    async def fetchone(self, *a, **k):
        return None


def _svc(llm, root) -> WorkspaceService:
    """A service with only the pieces the scan path touches."""
    notes = types.SimpleNamespace(project_id="prj_test")
    from rka.infra.file_access import FileAccessPolicy
    return WorkspaceService(
        db=_NoDuplicates(), academic_service=None, note_service=notes,
        literature_service=None, llm=llm,
        file_policy=FileAccessPolicy([root]),
    )


class _FakeLLM:
    """Fails for the named files, succeeds for the rest."""

    def __init__(self, failing: set[str] | None = None, always: bool = False):
        self.failing, self.always, self.calls = failing or set(), always, []

    async def classify_file(self, name, content, ext):
        self.calls.append(name)
        if self.always or name in self.failing:
            # Exactly what LLMClient.extract raises — for a refused connection
            # AND for a response that fails schema validation. It cannot tell
            # the caller which, and that is the point of the threshold.
            from rka.infra.llm import LLMUnavailableError

            raise LLMUnavailableError("LLM call failed: 1 validation error for FileClassification")
        return types.SimpleNamespace(
            confidence=0.9, content_type="general", journal_type="finding",
            tags=["a", "b"], title_suggestion=name,
        )


def _workspace(tmp_path, names):
    for n in names:
        (tmp_path / n).write_text(f"# {n}\n\nsome prose so the file is classified.\n")
    return str(tmp_path)


class TestOneBadFileDoesNotDisableTheScan:
    """The regression an adversarial review caught in the first version.

    `LLMClient.extract` reports a schema-invalid response and a refused
    connection as the same exception with the same message, so giving up on
    the first failure let one awkward file downgrade a healthy backend.
    """

    @pytest.mark.asyncio
    async def test_a_healthy_backend_keeps_being_asked_after_one_bad_file(self, tmp_path):
        llm = _FakeLLM(failing={"b.md"})
        root = _workspace(tmp_path, ["a.md", "b.md", "c.md", "d.md"])

        manifest = await _svc(llm, root).scan(root)

        assert sorted(llm.calls) == ["a.md", "b.md", "c.md", "d.md"], (
            "one file's invalid response must not stop the others being asked"
        )
        assert manifest.capabilities.llm_available is True
        classified = {f.relative_path: f.llm_classified for f in manifest.files}
        assert classified["c.md"] and classified["d.md"]
        assert not classified["b.md"]

    @pytest.mark.asyncio
    async def test_a_dead_backend_stops_after_the_threshold(self, tmp_path):
        llm = _FakeLLM(always=True)
        root = _workspace(tmp_path, [f"f{i}.md" for i in range(10)])

        manifest = await _svc(llm, root).scan(root)

        assert len(llm.calls) == _LLM_FAILURE_THRESHOLD, (
            f"a dead backend must cost {_LLM_FAILURE_THRESHOLD} files, not all "
            f"{len(manifest.files)} — max_files defaults to 5000"
        )
        assert manifest.capabilities.llm_available is False
        assert any("disabled for the rest of this scan" in w for w in manifest.warnings)

    def test_a_success_resets_the_streak(self):
        state, caps, warnings = _LlmScanHealth(), ScanCapabilities(llm_available=True), []
        for _ in range(_LLM_FAILURE_THRESHOLD - 1):
            WorkspaceService._record_llm_failure(
                state, caps, warnings, "classifying", "x.md", RuntimeError("bad")
            )
        state.record_success()
        WorkspaceService._record_llm_failure(
            state, caps, warnings, "classifying", "y.md", RuntimeError("bad")
        )
        assert caps.llm_available is True, "the streak must not survive a success"

    def test_the_state_is_per_scan_not_per_service(self):
        """Two scans can share a service instance."""
        assert not hasattr(WorkspaceService, "_llm_health")
        a, b = _LlmScanHealth(), _LlmScanHealth()
        a.consecutive_failures = 9
        assert b.consecutive_failures == 0


class TestTheScanSaysWhatHappened:
    @pytest.mark.asyncio
    async def test_a_failed_scan_does_not_return_an_empty_warnings_list(self, tmp_path):
        llm = _FakeLLM(always=True)
        manifest = await _svc(llm, tmp_path).scan(_workspace(tmp_path, ["a.md", "b.md", "c.md", "d.md"]))
        assert manifest.warnings, (
            "HTTP 200 with warnings=[] is the same response as a scan with no "
            "LLM configured at all"
        )
        assert "a.md" in " ".join(manifest.warnings)

    def test_the_failure_is_logged_where_it_can_be_seen(self, caplog):
        state, caps, warnings = _LlmScanHealth(), ScanCapabilities(llm_available=True), []
        with caplog.at_level(logging.WARNING, logger="rka.services.workspace"):
            WorkspaceService._record_llm_failure(
                state, caps, warnings, "classifying", "notes.md", RuntimeError("no route")
            )
        assert caplog.records, "debug is off by default, so the scan said nothing"
        assert "notes.md" in caplog.text and "no route" in caplog.text


class TestOneShapeForOneCondition:
    @pytest.mark.asyncio
    async def test_summarize_raises_the_error_the_app_handler_enriches(self):
        """A bare HTTPException(503) skips the handler that adds error/hint.

        Behavioural: call the route with llm=None and check which exception
        comes out, rather than grepping the module for a source line.
        """
        from fastapi import HTTPException

        from rka.api.routes.context import summarize
        from rka.infra.llm import LLMUnavailableError

        with pytest.raises(LLMUnavailableError):
            await summarize(
                data=types.SimpleNamespace(topic="x", entity_ids=None),
                project_id="prj_x", db=None, llm=None, search=None, note_svc=None,
            )

        # and specifically not the bare form the app handler never sees
        try:
            await summarize(
                data=types.SimpleNamespace(topic="x", entity_ids=None),
                project_id="prj_x", db=None, llm=None, search=None, note_svc=None,
            )
        except LLMUnavailableError as exc:
            assert not isinstance(exc, HTTPException)


class TestATimeoutSaysSomething:
    def test_it_renders_a_timeout_that_carries_no_request(self):
        """`exc.request` is a property that RAISES when unset.

        `getattr(exc, "request", None)` therefore does not fall back — it
        propagates, and the renderer crashed on exactly the case it exists to
        describe. The first version of this test only ever passed a timeout
        that had a request.
        """
        from rka.mcp import server

        out = json.loads(server._timeout_error(httpx.ReadTimeout("")))
        assert out["error"] == "api_timeout"
        assert "the API" in out["message"]

    def test_it_names_the_endpoint_when_there_is_one(self):
        from rka.mcp import server

        request = httpx.Request("POST", "http://localhost:9712/api/context")
        out = json.loads(server._timeout_error(httpx.ReadTimeout("", request=request)))
        assert out["kind"] == "ReadTimeout"
        assert "/api/context" in out["message"]
        assert "did not cancel" in out["message"], (
            "uvicorn does not cancel the handler on client disconnect, so the "
            "server keeps working; saying 'cancelled' would be a lie"
        )

    @pytest.mark.parametrize(
        "verb,target",
        [("rka_query", "_dispatch_query_typed"), ("rka_execute", "_dispatch_execute_typed")],
    )
    @pytest.mark.asyncio
    async def test_both_verbs_render_a_timeout_instead_of_raising(
        self, verb, target, monkeypatch
    ):
        """Behavioural: the previous version grepped for the phrase, so it
        passed with both guards deleted as long as a comment kept the words."""
        from rka.mcp import server

        async def _boom(*a, **k):
            raise httpx.ReadTimeout("")

        monkeypatch.setattr(server, target, _boom)
        fn = getattr(server, verb)
        out = json.loads(await fn(types.SimpleNamespace(operation="status", project_id="prj_x")))
        assert out["error"] == "api_timeout"

    def test_health_never_answers_unhealthy_without_a_reason(self):
        """The one operation whose whole job is reporting reachability.

        Its own `except Exception` runs inside the verb, so the verb-level
        timeout guard never sees it, and `str(ReadTimeout())` is empty.
        """
        from rka.mcp import verb_dispatch

        src = __import__("inspect").getsource(verb_dispatch.dispatch_session)
        assert 'or f"{type(exc).__name__} (no message)"' in src
