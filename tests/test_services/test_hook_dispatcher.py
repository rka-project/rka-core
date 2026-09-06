"""Unit tests for HookDispatcher — depth cap, handler dispatch, failure silencing."""

from __future__ import annotations

import json

import pytest
import pytest_asyncio

from rka.infra.database import Database
from rka.services.hook_dispatcher import HookDispatcher, MAX_DEPTH


async def _register_hook(
    db: Database,
    *,
    hook_id: str,
    event: str,
    handler_type: str,
    handler_config: dict,
    enabled: bool = True,
    name: str = "test-hook",
    project_id: str = "proj_default",
) -> None:
    await db.execute(
        """INSERT INTO hooks
           (id, event, project_id, handler_type, handler_config, enabled, name, created_by)
           VALUES (?, ?, ?, ?, ?, ?, ?, 'pi')""",
        [hook_id, event, project_id, handler_type, json.dumps(handler_config),
         1 if enabled else 0, name],
    )
    await db.commit()


@pytest_asyncio.fixture
async def dispatcher(db: Database) -> HookDispatcher:
    return HookDispatcher(db)


# ----------------------------------------------------------------- brain_notify


class TestBrainNotify:
    @pytest.mark.asyncio
    async def test_brain_notify_writes_row(self, db: Database, dispatcher: HookDispatcher):
        await _register_hook(
            db,
            hook_id="hk_bn_1",
            event="session_start",
            handler_type="brain_notify",
            handler_config={
                "severity": "info",
                "content_template": {"message": "hello {project_id}"},
            },
        )
        ids = await dispatcher.fire(
            event="session_start",
            payload={"project_id": "proj_default", "actor": "brain"},
            project_id="proj_default",
        )
        assert len(ids) == 1

        # One brain_notification row created.
        rows = await db.fetchall(
            "SELECT content, severity FROM brain_notifications WHERE hook_id = ?",
            ["hk_bn_1"],
        )
        assert len(rows) == 1
        content = json.loads(rows[0]["content"])
        assert content == {"message": "hello proj_default"}
        assert rows[0]["severity"] == "info"

        # Execution logged as success.
        ex = await db.fetchall(
            "SELECT status FROM hook_executions WHERE hook_id = ?", ["hk_bn_1"],
        )
        assert ex[0]["status"] == "success"

    @pytest.mark.asyncio
    async def test_brain_notify_severity_defaults_to_info_when_invalid(
        self, db: Database, dispatcher: HookDispatcher,
    ):
        await _register_hook(
            db,
            hook_id="hk_bn_2",
            event="session_start",
            handler_type="brain_notify",
            handler_config={"severity": "apocalyptic", "content_template": {"k": "v"}},
        )
        await dispatcher.fire("session_start", {}, "proj_default")
        row = await db.fetchone(
            "SELECT severity FROM brain_notifications WHERE hook_id = ?", ["hk_bn_2"],
        )
        assert row["severity"] == "info"


# ------------------------------------------------------------------------- sql


class TestSqlHandler:
    @pytest.mark.asyncio
    async def test_legacy_sql_handler_cannot_run_even_parameterized_insert(
        self, db: Database, dispatcher: HookDispatcher,
    ):
        # Seed an old hook directly: upgrades/imports retain these rows.
        await _register_hook(
            db,
            hook_id="hk_sql_1",
            event="post_journal_create",
            handler_type="sql",
            handler_config={
                "statement": (
                    "INSERT INTO audit_log (action, entity_type, entity_id, actor, details) "
                    "VALUES ('enrich', 'journal', ?, 'system', ?)"
                ),
                "params": ["{entry_id}", "{note}"],
            },
        )
        await dispatcher.fire(
            event="post_journal_create",
            payload={"entry_id": "jrn_test1", "note": "hooked"},
            project_id="proj_default",
        )
        rows = await db.fetchall(
            "SELECT entity_id, details FROM audit_log WHERE entity_id = 'jrn_test1'",
        )
        assert rows == []
        execution = await db.fetchone(
            "SELECT status, error_message FROM hook_executions WHERE hook_id = ?", ["hk_sql_1"],
        )
        assert execution["status"] == "error"
        assert "unsupported" in execution["error_message"]

    @pytest.mark.asyncio
    async def test_sql_handler_missing_statement_logs_error(
        self, db: Database, dispatcher: HookDispatcher,
    ):
        await _register_hook(
            db,
            hook_id="hk_sql_bad",
            event="periodic",
            handler_type="sql",
            handler_config={"params": ["x"]},  # no 'statement'
        )
        await dispatcher.fire("periodic", {}, "proj_default")
        row = await db.fetchone(
            "SELECT status, error_message FROM hook_executions WHERE hook_id = ?",
            ["hk_sql_bad"],
        )
        assert row["status"] == "error"
        assert "unsupported" in row["error_message"]

    @pytest.mark.asyncio
    async def test_sql_handler_failure_silently_logged(
        self, db: Database, dispatcher: HookDispatcher,
    ):
        """A broken SQL statement logs error status but never raises."""
        await _register_hook(
            db,
            hook_id="hk_sql_broken",
            event="periodic",
            handler_type="sql",
            handler_config={"statement": "INSERT INTO nowhere_table VALUES (?)", "params": [1]},
        )
        # Should not raise even though the INSERT will fail.
        await dispatcher.fire("periodic", {}, "proj_default")
        row = await db.fetchone(
            "SELECT status, error_message FROM hook_executions WHERE hook_id = ?",
            ["hk_sql_broken"],
        )
        assert row["status"] == "error"
        assert row["error_message"] is not None


# -------------------------------------------------------------------- mcp_tool


class TestMcpToolHandler:
    @pytest.mark.asyncio
    async def test_legacy_mcp_tool_handler_does_not_claim_execution(
        self, db: Database, dispatcher: HookDispatcher,
    ):
        await _register_hook(
            db,
            hook_id="hk_mcp_1",
            event="post_claim_extract",
            handler_type="mcp_tool",
            handler_config={
                "tool": "rka_detect_contradictions",
                "args": {"entity_id": "{claim_ids}"},
            },
        )
        await dispatcher.fire(
            event="post_claim_extract",
            payload={"claim_ids": ["clm_a", "clm_b"]},
            project_id="proj_default",
        )
        row = await db.fetchone(
            "SELECT status, handler_result FROM hook_executions WHERE hook_id = ?",
            ["hk_mcp_1"],
        )
        assert row["status"] == "error"
        assert row["handler_result"] is None

    @pytest.mark.asyncio
    async def test_mcp_tool_handler_missing_tool_logs_error(
        self, db: Database, dispatcher: HookDispatcher,
    ):
        await _register_hook(
            db,
            hook_id="hk_mcp_bad",
            event="periodic",
            handler_type="mcp_tool",
            handler_config={"args": {}},  # no 'tool'
        )
        await dispatcher.fire("periodic", {}, "proj_default")
        row = await db.fetchone(
            "SELECT status, error_message FROM hook_executions WHERE hook_id = ?",
            ["hk_mcp_bad"],
        )
        assert row["status"] == "error"


# ------------------------------------------------------------------- dispatch


class TestDispatchRules:
    @pytest.mark.asyncio
    async def test_disabled_hook_is_not_selected(
        self, db: Database, dispatcher: HookDispatcher,
    ):
        await _register_hook(
            db, hook_id="hk_off", event="session_start",
            handler_type="brain_notify",
            handler_config={"content_template": {"x": 1}},
            enabled=False,
        )
        ids = await dispatcher.fire("session_start", {}, "proj_default")
        assert ids == []
        # No execution row created — disabled hooks are simply not selected.
        count = (await db.fetchone(
            "SELECT COUNT(*) AS c FROM hook_executions WHERE hook_id = ?", ["hk_off"],
        ))["c"]
        assert count == 0

    @pytest.mark.asyncio
    async def test_project_scoping_isolates_hooks(
        self, db: Database, dispatcher: HookDispatcher,
    ):
        await _register_hook(
            db, hook_id="hk_proj_a", event="session_start",
            handler_type="brain_notify",
            handler_config={"content_template": {"p": "A"}},
            project_id="proj_a",
        )
        await _register_hook(
            db, hook_id="hk_proj_b", event="session_start",
            handler_type="brain_notify",
            handler_config={"content_template": {"p": "B"}},
            project_id="proj_b",
        )
        # Fire for proj_a — only hk_proj_a should execute.
        await dispatcher.fire("session_start", {}, "proj_a")
        fired = await db.fetchall(
            "SELECT hook_id FROM hook_executions ORDER BY hook_id",
        )
        assert [r["hook_id"] for r in fired] == ["hk_proj_a"]

    @pytest.mark.asyncio
    async def test_event_filter_isolates_hooks(
        self, db: Database, dispatcher: HookDispatcher,
    ):
        await _register_hook(
            db, hook_id="hk_sess", event="session_start",
            handler_type="brain_notify",
            handler_config={"content_template": {}},
        )
        await _register_hook(
            db, hook_id="hk_note", event="post_journal_create",
            handler_type="brain_notify",
            handler_config={"content_template": {}},
        )
        await dispatcher.fire("post_journal_create", {}, "proj_default")
        fired = await db.fetchall(
            "SELECT hook_id FROM hook_executions",
        )
        assert [r["hook_id"] for r in fired] == ["hk_note"]

    @pytest.mark.asyncio
    async def test_payload_passes_through_to_handler(
        self, db: Database, dispatcher: HookDispatcher,
    ):
        await _register_hook(
            db, hook_id="hk_payload", event="post_record_outcome",
            handler_type="brain_notify",
            handler_config={
                "content_template": {
                    "decision": "{decision_id}",
                    "outcome": "{outcome}",
                }
            },
        )
        await dispatcher.fire(
            "post_record_outcome",
            {"decision_id": "dec_X", "outcome": "succeeded"},
            "proj_default",
        )
        row = await db.fetchone(
            "SELECT content FROM brain_notifications WHERE hook_id = ?",
            ["hk_payload"],
        )
        content = json.loads(row["content"])
        assert content == {"decision": "dec_X", "outcome": "succeeded"}


# ------------------------------------------------------------------ depth cap


class TestDepthCap:
    @pytest.mark.asyncio
    async def test_depth_zero_executes_normally(self, db: Database, dispatcher: HookDispatcher):
        await _register_hook(
            db, hook_id="hk_d0", event="session_start",
            handler_type="brain_notify",
            handler_config={"content_template": {}},
        )
        ids = await dispatcher.fire("session_start", {}, "proj_default", depth=0)
        assert len(ids) == 1
        row = await db.fetchone(
            "SELECT status FROM hook_executions WHERE hook_id = ?", ["hk_d0"],
        )
        assert row["status"] == "success"

    @pytest.mark.asyncio
    async def test_depth_at_max_aborts_with_logged_status(
        self, db: Database, dispatcher: HookDispatcher,
    ):
        await _register_hook(
            db, hook_id="hk_deep", event="post_journal_create",
            handler_type="brain_notify",
            handler_config={"content_template": {}},
        )
        ids = await dispatcher.fire(
            "post_journal_create", {}, "proj_default", depth=MAX_DEPTH,
        )
        assert len(ids) == 1
        row = await db.fetchone(
            "SELECT status, error_message, depth FROM hook_executions WHERE hook_id = ?",
            ["hk_deep"],
        )
        assert row["status"] == "aborted_depth_limit"
        assert "MAX_DEPTH" in row["error_message"]
        assert row["depth"] == MAX_DEPTH
        # No notification written when aborted.
        count = (await db.fetchone(
            "SELECT COUNT(*) AS c FROM brain_notifications WHERE hook_id = ?", ["hk_deep"],
        ))["c"]
        assert count == 0

    @pytest.mark.asyncio
    async def test_depth_cap_logs_per_matching_hook(
        self, db: Database, dispatcher: HookDispatcher,
    ):
        """When firing at depth >= MAX_DEPTH, every matching enabled hook gets
        one aborted_depth_limit row so the cascade is visible in the audit log."""
        for i in range(3):
            await _register_hook(
                db, hook_id=f"hk_cap_{i}", event="periodic",
                handler_type="brain_notify",
                handler_config={"content_template": {}},
            )
        ids = await dispatcher.fire("periodic", {}, "proj_default", depth=MAX_DEPTH)
        assert len(ids) == 3
        rows = await db.fetchall(
            "SELECT status FROM hook_executions WHERE status = 'aborted_depth_limit'",
        )
        assert len(rows) == 3


# ------------------------------------------------------------------ no-matching


@pytest.mark.asyncio
async def test_fire_with_no_registered_hooks_returns_empty(
    dispatcher: HookDispatcher,
):
    ids = await dispatcher.fire("session_start", {}, "proj_default")
    assert ids == []


@pytest.mark.asyncio
async def test_fire_multiple_hooks_same_event_all_execute(
    db: Database, dispatcher: HookDispatcher,
):
    for i in range(3):
        await _register_hook(
            db, hook_id=f"hk_m_{i}", event="session_start",
            handler_type="brain_notify",
            handler_config={"content_template": {"i": i}},
        )
    ids = await dispatcher.fire("session_start", {}, "proj_default")
    assert len(ids) == 3
    count = (await db.fetchone(
        "SELECT COUNT(*) AS c FROM hook_executions WHERE status = 'success'",
    ))["c"]
    assert count == 3


# ----------------------------------------------------------- interpolation


class TestContentInterpolation:
    def test_plain_string_key_returns_payload_value(self):
        out = HookDispatcher._interpolate_content("{x}", {"x": 42})
        assert out == 42

    def test_partial_string_substitution(self):
        out = HookDispatcher._interpolate_content("id={x}", {"x": "clm_1"})
        assert out == "id=clm_1"

    def test_dict_and_list_recursed(self):
        tmpl = {"m": "{msg}", "ids": ["{a}", "{b}"]}
        out = HookDispatcher._interpolate_content(
            tmpl, {"msg": "hi", "a": 1, "b": 2},
        )
        assert out == {"m": "hi", "ids": [1, 2]}

    def test_missing_key_returns_template_unchanged(self):
        out = HookDispatcher._interpolate_content("value={notpresent}", {"x": 1})
        assert out == "value={notpresent}"

    def test_non_string_types_pass_through(self):
        assert HookDispatcher._interpolate_content(42, {}) == 42
        assert HookDispatcher._interpolate_content(True, {}) is True
