#!/usr/bin/env python3
"""Generate or verify the reviewable RKA Core v1 contract snapshots.

The default mode is deliberately read-only and fails when generated content
differs from the checked-in files.  An intentional public-contract change must
use ``--write`` and include the resulting JSON diff in review.
"""

from __future__ import annotations

import argparse
import difflib
import json
import copy
import os
import sys
from collections import Counter
from pathlib import Path
from typing import Annotated, Any, get_args, get_origin


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


# Keep the default seven-tool transport deterministic even when a developer has
# enabled optional local tool surfaces in their shell.
os.environ["RKA_LEGACY_TOOLS"] = "0"
os.environ["RKA_SKILL_TOOLS"] = "0"

from rka.api.app import create_app  # noqa: E402
from rka.contracts import (  # noqa: E402
    CORE,
    MCP_TRANSPORT_TOOLS,
    mcp_operation_disposition,
    rest_operation_disposition,
    rest_operation_maturity,
)
from rka.mcp.operation_args import ExecuteArgsUnion, QueryArgsUnion  # noqa: E402
from rka.mcp.operations_schema import (  # noqa: E402
    OPERATIONS_SCHEMA,
    WRITER_COMPATIBILITY_OPERATIONS,
    operation_maturity,
)
from rka.mcp.server import mcp  # noqa: E402


REST_SNAPSHOT = ROOT / "contracts" / "rka-rest-v1.openapi.json"
MCP_SNAPSHOT = ROOT / "contracts" / "rka-mcp-v1.json"
HTTP_METHODS = frozenset({"get", "put", "post", "delete", "options", "head", "patch", "trace"})
NOISE_KEYS = frozenset({"description", "summary", "title", "example", "examples"})
NAMED_MAP_KEYS = frozenset(
    {
        "$defs",
        "callbacks",
        "content",
        "definitions",
        "dependentSchemas",
        "headers",
        "links",
        "mapping",
        "patternProperties",
        "properties",
        "responses",
        "schemas",
    }
)


def _normalize(value: Any, *, named_map: bool = False) -> Any:
    """Remove prose-only churn while retaining every wire-level constraint."""

    if isinstance(value, dict):
        return {
            key: _normalize(item, named_map=key in NAMED_MAP_KEYS)
            for key, item in sorted(value.items())
            if named_map or (key not in NOISE_KEYS and not key.startswith("x-"))
        }
    if isinstance(value, list):
        return [_normalize(item) for item in value]
    return value


def _rewrite_component_refs(value: Any, aliases: dict[str, str]) -> None:
    """Rewrite schema refs in place after equivalent aliases are collapsed."""

    if isinstance(value, dict):
        ref = value.get("$ref")
        prefix = "#/components/schemas/"
        if isinstance(ref, str) and ref.startswith(prefix):
            name = ref[len(prefix) :]
            if name in aliases:
                value["$ref"] = f"{prefix}{aliases[name]}"
        for item in value.values():
            _rewrite_component_refs(item, aliases)
    elif isinstance(value, list):
        for item in value:
            _rewrite_component_refs(item, aliases)


def _canonicalize_openapi_io_schemas(openapi: dict[str, Any]) -> dict[str, Any]:
    """Collapse equivalent Pydantic ``-Input``/``-Output`` schema aliases.

    FastAPI/Pydantic releases differ in whether an identical request/response
    model is emitted once as ``Model`` or twice as ``Model-Input`` and
    ``Model-Output``. This is generator noise rather than a wire-contract
    difference. Only byte-equivalent normalized pairs are collapsed.
    """

    canonical = copy.deepcopy(openapi)
    schemas = canonical.get("components", {}).get("schemas", {})
    aliases: dict[str, str] = {}
    for name in tuple(schemas):
        if not name.endswith("-Input"):
            continue
        base = name.removesuffix("-Input")
        output_name = f"{base}-Output"
        if output_name not in schemas:
            continue
        input_schema = schemas[name]
        output_schema = schemas[output_name]
        if _normalize(input_schema) != _normalize(output_schema):
            continue
        if base in schemas and _normalize(schemas[base]) != _normalize(input_schema):
            continue
        schemas.setdefault(base, input_schema)
        aliases[name] = base
        aliases[output_name] = base
        del schemas[name]
        del schemas[output_name]
    if aliases:
        _rewrite_component_refs(canonical, aliases)
    return canonical


def _collect_schema_refs(value: Any) -> set[str]:
    refs: set[str] = set()
    if isinstance(value, dict):
        ref = value.get("$ref")
        prefix = "#/components/schemas/"
        if isinstance(ref, str) and ref.startswith(prefix):
            refs.add(ref[len(prefix) :])
        for item in value.values():
            refs.update(_collect_schema_refs(item))
    elif isinstance(value, list):
        for item in value:
            refs.update(_collect_schema_refs(item))
    return refs


def _reachable_openapi_schemas(openapi: dict[str, Any], paths: dict[str, Any]) -> dict[str, Any]:
    all_schemas = openapi.get("components", {}).get("schemas", {})
    pending = list(_collect_schema_refs(paths))
    reachable: dict[str, Any] = {}
    while pending:
        name = pending.pop()
        if name in reachable:
            continue
        if name not in all_schemas:
            raise RuntimeError(f"OpenAPI references missing component schema {name!r}")
        schema = all_schemas[name]
        reachable[name] = schema
        pending.extend(_collect_schema_refs(schema) - reachable.keys())
    return dict(sorted(reachable.items()))


def _ensure_response_descriptions(paths: dict[str, Any]) -> None:
    """Restore the required OpenAPI Response Object description deterministically.

    Prose descriptions are otherwise removed from snapshots to avoid cosmetic
    churn. OpenAPI 3.1 nevertheless requires every inline Response Object to
    contain a description, so the snapshot uses a fixed non-semantic value.
    """

    for path_item in paths.values():
        if not isinstance(path_item, dict):
            continue
        for method, operation in path_item.items():
            if method not in HTTP_METHODS or not isinstance(operation, dict):
                continue
            responses = operation.get("responses", {})
            if not isinstance(responses, dict):
                continue
            for response in responses.values():
                if isinstance(response, dict) and "$ref" not in response:
                    response.setdefault("description", "Response")


def build_rest_snapshot() -> dict[str, Any]:
    openapi = _canonicalize_openapi_io_schemas(create_app().openapi())
    stable_paths: dict[str, dict[str, Any]] = {}
    inventory: dict[str, list[dict[str, str]]] = {
        "core-preview": [],
        "writer-compatibility": [],
        "agentic-unsupported": [],
        "core-legacy": [],
    }
    counts: Counter[str] = Counter()

    for path, path_item in sorted(openapi["paths"].items()):
        if not path.startswith("/api"):
            continue
        for method, operation in sorted(path_item.items()):
            if method not in HTTP_METHODS:
                continue
            method_upper = method.upper()
            disposition = rest_operation_disposition(
                method_upper,
                path,
                deprecated=bool(operation.get("deprecated")),
            )
            counts[disposition] += 1
            identity = {
                "method": method_upper,
                "operation_id": operation["operationId"],
                "path": path,
            }
            if disposition != CORE:
                inventory[disposition].append(identity)
                continue

            maturity = rest_operation_maturity(
                method_upper,
                path,
                tags=operation.get("tags", []),
            )
            counts[f"core-{maturity}"] += 1
            if maturity == "preview":
                inventory["core-preview"].append(identity)
                continue

            stable_paths.setdefault(path, {})[method] = operation

        # OpenAPI permits parameters and servers at path-item scope. FastAPI
        # does not emit them today, but if that changes they are part of every
        # stable method on the path and must not disappear from the snapshot.
        if path in stable_paths:
            for key, value in path_item.items():
                if key not in HTTP_METHODS:
                    stable_paths[path][key] = value

    normalized_paths = _normalize(stable_paths)
    _ensure_response_descriptions(normalized_paths)
    schemas = _reachable_openapi_schemas(openapi, stable_paths)
    inventory = {
        name: sorted(items, key=lambda item: (item["path"], item["method"]))
        for name, items in inventory.items()
    }
    return {
        "openapi": openapi["openapi"],
        "info": {"title": "RKA Core stable REST contract", "version": "v1"},
        "x-rka-contract": "rka-rest/v1",
        "x-rka-snapshot-format": "rka.contract-snapshot/v1",
        "x-rka-operation-counts": {
            "ownership": {
                name: counts[name]
                for name in (
                    "core",
                    "writer-compatibility",
                    "agentic-unsupported",
                    "core-legacy",
                )
            },
            "core_maturity": {
                "stable": counts["core-stable"],
                "preview": counts["core-preview"],
            },
        },
        "x-rka-non-stable-inventory": inventory,
        "paths": normalized_paths,
        "components": {
            **{
                key: _normalize(value)
                for key, value in sorted(openapi.get("components", {}).items())
                if key != "schemas"
            },
            "schemas": _normalize(schemas, named_map=True),
        },
        **{
            key: _normalize(openapi[key])
            for key in ("jsonSchemaDialect", "security", "servers")
            if key in openapi
        },
    }


def _union_models(annotation: Any) -> list[type]:
    while get_origin(annotation) is Annotated:
        annotation = get_args(annotation)[0]
    return list(get_args(annotation))


def _operation_models() -> dict[str, type]:
    models: dict[str, type] = {}
    for model in _union_models(QueryArgsUnion) + _union_models(ExecuteArgsUnion):
        operation = model.model_fields["operation"].default
        if operation in models:
            raise RuntimeError(f"duplicate MCP argument model for {operation!r}")
        models[operation] = model
    expected = set(OPERATIONS_SCHEMA)
    actual = set(models)
    if actual != expected:
        raise RuntimeError(
            "typed MCP model/schema drift: "
            f"missing={sorted(expected - actual)}, extra={sorted(actual - expected)}"
        )
    return models


def _collect_local_defs(value: Any) -> set[str]:
    refs: set[str] = set()
    if isinstance(value, dict):
        ref = value.get("$ref")
        prefix = "#/$defs/"
        if isinstance(ref, str) and ref.startswith(prefix):
            refs.add(ref[len(prefix) :])
        for item in value.values():
            refs.update(_collect_local_defs(item))
    elif isinstance(value, list):
        for item in value:
            refs.update(_collect_local_defs(item))
    return refs


def _stable_transport_schema(schema: dict[str, Any], stable_operations: set[str]) -> dict[str, Any]:
    """Project a multiplexed transport schema onto stable operation branches."""

    projected = copy.deepcopy(schema)
    args = projected.get("properties", {}).get("args")
    if not isinstance(args, dict) or "discriminator" not in args:
        return projected

    mapping = args["discriminator"].get("mapping", {})
    stable_mapping = {name: ref for name, ref in mapping.items() if name in stable_operations}
    args["discriminator"]["mapping"] = stable_mapping
    stable_refs = set(stable_mapping.values())
    args["oneOf"] = [
        branch for branch in args.get("oneOf", []) if branch.get("$ref") in stable_refs
    ]

    all_defs = projected.get("$defs", {})
    pending = list(_collect_local_defs(args))
    reachable: dict[str, Any] = {}
    while pending:
        name = pending.pop()
        if name in reachable:
            continue
        if name not in all_defs:
            raise RuntimeError(f"MCP schema references missing $defs entry {name!r}")
        definition = all_defs[name]
        reachable[name] = definition
        pending.extend(_collect_local_defs(definition) - reachable.keys())
    projected["$defs"] = dict(sorted(reachable.items()))
    return projected


def build_mcp_snapshot() -> dict[str, Any]:
    models = _operation_models()
    counts: Counter[str] = Counter()
    stable_operations: dict[str, Any] = {}
    inventory: dict[str, list[dict[str, str]]] = {
        "core-preview": [],
        "writer-compatibility": [],
        "agentic-unsupported": [],
        "core-legacy": [],
    }

    for name, entry in sorted(OPERATIONS_SCHEMA.items()):
        disposition = mcp_operation_disposition(
            name,
            writer_operations=WRITER_COMPATIBILITY_OPERATIONS,
        )
        counts[disposition] += 1
        identity = {
            "category": entry["category"],
            "operation": name,
            "tool": entry["tool"],
        }
        if disposition != CORE:
            inventory[disposition].append(identity)
            continue

        maturity = operation_maturity(name)
        counts[f"core-{maturity}"] += 1
        if maturity == "preview":
            inventory["core-preview"].append(identity)
            continue
        if maturity != "stable":
            raise RuntimeError(f"unknown MCP maturity {maturity!r} for {name!r}")
        stable_operations[name] = {
            "category": entry["category"],
            "input_schema": _normalize(models[name].model_json_schema()),
            "tool": entry["tool"],
        }

    tools = {tool.name: tool for tool in mcp._tool_manager.list_tools()}
    missing_tools = set(MCP_TRANSPORT_TOOLS) - set(tools)
    if missing_tools:
        raise RuntimeError(f"missing public MCP transport tools: {sorted(missing_tools)}")
    unexpected_tools = set(tools) - set(MCP_TRANSPORT_TOOLS)
    if unexpected_tools:
        raise RuntimeError(
            f"default MCP transport exposed unexpected tools: {sorted(unexpected_tools)}"
        )
    stable_names = set(stable_operations)
    transport_tools = {
        name: {
            "input_schema": _normalize(
                _stable_transport_schema(tools[name].parameters, stable_names)
            ),
            "output_schema": _normalize(tools[name].fn_metadata.output_schema),
        }
        for name in MCP_TRANSPORT_TOOLS
    }

    return {
        "contract": "rka-mcp/v1",
        "format": "rka.contract-snapshot/v1",
        "operation_counts": {
            "ownership": {
                name: counts[name]
                for name in (
                    "core",
                    "writer-compatibility",
                    "agentic-unsupported",
                    "core-legacy",
                )
            },
            "core_maturity": {
                "stable": counts["core-stable"],
                "preview": counts["core-preview"],
            },
        },
        "non_stable_inventory": {
            name: sorted(items, key=lambda item: item["operation"])
            for name, items in inventory.items()
        },
        "operations": stable_operations,
        "transport_tools": transport_tools,
    }


def _render(snapshot: dict[str, Any]) -> str:
    return json.dumps(snapshot, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def _check(path: Path, rendered: str) -> bool:
    if not path.exists():
        print(f"missing contract snapshot: {path.relative_to(ROOT)}")
        return False
    current = path.read_text(encoding="utf-8")
    if current == rendered:
        print(f"ok: {path.relative_to(ROOT)}")
        return True
    print(f"contract snapshot drift: {path.relative_to(ROOT)}")
    diff = difflib.unified_diff(
        current.splitlines(),
        rendered.splitlines(),
        fromfile=str(path.relative_to(ROOT)),
        tofile="generated",
        lineterm="",
    )
    for index, line in enumerate(diff):
        if index >= 200:
            print("... diff truncated; run with --write and inspect git diff")
            break
        print(line)
    return False


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--write",
        action="store_true",
        help="replace checked-in snapshots with the current public schemas",
    )
    args = parser.parse_args()

    outputs = {
        REST_SNAPSHOT: _render(build_rest_snapshot()),
        MCP_SNAPSHOT: _render(build_mcp_snapshot()),
    }
    if args.write:
        for path, rendered in outputs.items():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(rendered, encoding="utf-8")
            print(f"wrote: {path.relative_to(ROOT)}")
        return 0

    return 0 if all(_check(path, rendered) for path, rendered in outputs.items()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
