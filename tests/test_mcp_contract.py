"""MCP contract tests (task 3.8): a real MCP client calls every tool in-process.

- exactly the five PRD §2.3 tools, all advertising the pinned schema version
- advertised input/output schemas equal the pinned files in schemas/mcp/v1/
  (regenerate deliberately with `warehouse-graph mcp-schemas --write`, which is a contract change)
- every tool's structured output validates against its pinned output schema
- the caller's principal decides what exists: hidden == not found
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from functools import partial
from pathlib import Path
from typing import Any

import jsonschema
import pytest
from mcp import Client
from mcp.server.mcpserver import MCPServer
from mcp.types import CallToolResult, TextContent

from warehouse_graph.governance import load_policies
from warehouse_graph.graph import DbtArtifacts, GraphArtifact, build
from warehouse_graph.graph.embed import HashEmbedder
from warehouse_graph.mcp_server import (
    SCHEMA_DIR,
    SCHEMA_VERSION,
    TOOLS,
    create_server,
    tool_schemas,
)

FIXTURE = Path(__file__).parent / "fixtures" / "dbt_mini"

CALLS: dict[str, dict[str, Any]] = {
    "search_nodes": {"query": "ad spend", "k": 5},
    "get_node": {"id": "model.fct_ad_spend_daily"},
    "get_lineage": {"id": "model.fct_ad_spend_daily", "direction": "both", "depth": 3},
    "get_blast_radius": {"id": "source.marketing.ad_spend"},
    "get_metric": {"name": "ad_spend_nzd"},
}


@pytest.fixture(scope="module")
def graph() -> GraphArtifact:
    return build(
        DbtArtifacts(FIXTURE),
        HashEmbedder(),
        caveats=FIXTURE / "caveats.yml",
        query_examples=FIXTURE / "query_examples.yml",
    )


def _server(graph: GraphArtifact, identity: str | None = "admin@example") -> MCPServer:
    policies = load_policies(FIXTURE / "policies.yml")
    return create_server(graph, policies, identity=identity, embedder=HashEmbedder())


def _run[T](server: MCPServer, fn: Callable[[Client], Awaitable[T]]) -> T:
    async def go() -> T:
        async with Client(server) as client:
            return await fn(client)

    return asyncio.run(go())


async def _call(c: Client, tool: str, args: dict[str, Any]) -> CallToolResult:
    return await c.call_tool(tool, args)


def _text(result: CallToolResult) -> str:
    content = result.content[0]
    assert isinstance(content, TextContent)
    return content.text


def _pinned(name: str, kind: str) -> dict[str, Any]:
    data: dict[str, Any] = json.loads((SCHEMA_DIR / f"{name}.{kind}.json").read_text("utf-8"))
    return data


def test_exactly_the_five_tools(graph: GraphArtifact) -> None:
    tools = _run(_server(graph), lambda c: c.list_tools())
    assert sorted(t.name for t in tools.tools) == sorted(TOOLS)
    assert len(TOOLS) == 5  # PRD §1.4.1: a ceiling, not a target
    assert all((t.meta or {}).get("schema_version") == SCHEMA_VERSION for t in tools.tools)


def test_advertised_schemas_match_pinned_files(graph: GraphArtifact) -> None:
    schemas = asyncio.run(tool_schemas(_server(graph)))
    for name in TOOLS:
        assert schemas[name]["input"] == _pinned(name, "input"), f"{name} input schema changed"
        assert schemas[name]["output"] == _pinned(name, "output"), f"{name} output schema changed"
    assert len(list(SCHEMA_DIR.glob("*.json"))) == 2 * len(TOOLS)


@pytest.mark.parametrize("tool", TOOLS)
def test_tool_output_validates_against_pinned_schema(graph: GraphArtifact, tool: str) -> None:
    result = _run(_server(graph), lambda c: c.call_tool(tool, CALLS[tool]))
    assert not result.is_error, result.content
    jsonschema.validate(result.structured_content, _pinned(tool, "output"))


def test_invalid_arguments_are_rejected(graph: GraphArtifact) -> None:
    cases: list[tuple[str, dict[str, Any]]] = [
        ("search_nodes", {"query": "x", "k": 500}),
        ("get_lineage", {"id": "model.orders_v1", "direction": "sideways"}),
        ("get_lineage", {"id": "model.orders_v1", "depth": 99}),
    ]
    for tool, args in cases:
        result = _run(_server(graph), partial(_call, tool=tool, args=args))
        assert result.is_error, (tool, args)


def test_hidden_node_is_not_found_like_a_missing_one(graph: GraphArtifact) -> None:
    server = _server(graph, identity="marketing@example")
    texts = []
    for node_id in ["column.stg_hr__employees.annual_salary", "column.stg_hr__employees.nope"]:
        result = _run(server, partial(_call, tool="get_node", args={"id": node_id}))
        assert result.is_error
        texts.append(_text(result).replace(node_id, "<id>"))
    assert texts[0] == texts[1]


def test_principal_changes_what_tools_return(graph: GraphArtifact) -> None:
    args = {"id": "model.stg_hr__employees"}
    admin = _run(_server(graph), lambda c: c.call_tool("get_node", args))
    marketing = _run(_server(graph, "marketing@example"), lambda c: c.call_tool("get_node", args))
    cols = [{c["name"] for c in r.structured_content["columns"]} for r in (admin, marketing)]
    assert "annual_salary" in cols[0] and "annual_salary" not in cols[1]
    assert "work_email" not in cols[1]


def test_unknown_identity_is_refused_at_startup(graph: GraphArtifact) -> None:
    with pytest.raises(PermissionError):
        _server(graph, identity="stranger@example")


def test_no_identity_means_no_answers(graph: GraphArtifact) -> None:
    result = _run(
        _server(graph, identity=None), lambda c: c.call_tool("get_node", CALLS["get_node"])
    )
    assert result.is_error
    assert "identity" in _text(result)


class _Recorder:
    def __init__(self) -> None:
        self.events: list[tuple[str, str, dict[str, Any]]] = []

    def write(self, principal: str, event_type: str, payload: dict[str, Any]) -> int:
        self.events.append((principal, event_type, payload))
        return len(self.events)


def test_tool_calls_are_audited(graph: GraphArtifact) -> None:
    recorder = _Recorder()
    server = create_server(
        graph,
        load_policies(FIXTURE / "policies.yml"),
        identity="au@example",
        embedder=HashEmbedder(),
        audit=recorder,  # type: ignore[arg-type]
    )

    async def calls(c: Client) -> None:
        await c.call_tool("get_metric", {"name": "ad_spend_nzd"})
        await c.call_tool("get_node", {"id": "model.orders_v1"})  # hidden for analyst_au

    _run(server, calls)
    assert [(p, e) for p, e, _ in recorder.events] == [
        ("analyst_au", "mcp.tool_call"),
        ("analyst_au", "mcp.not_found"),
    ]
    assert recorder.events[0][2] == {"tool": "get_metric", "args": {"name": "ad_spend_nzd"}}
