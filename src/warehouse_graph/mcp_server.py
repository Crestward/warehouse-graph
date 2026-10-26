"""The warehouse-graph MCP server (task 3.7): exactly five read-only tools (PRD §2.3).

    search_nodes, get_node, get_lineage, get_blast_radius, get_metric

Both projects reach the graph only through this server. Every call runs against the graph as
the caller's principal is allowed to see it (governance.metadata), so a hidden table or column
can't be found, read, traversed to or confirmed: asking for it gives the same "not found" as
asking for something that doesn't exist.

Who is calling: over an authenticated transport (streamable HTTP with a token verifier) the
token's subject is the identity. Over stdio there is no token, so the host application, which
has already authenticated its user, starts the server bound to that user's identity
(`warehouse-graph mcp --identity ...`). Either way the identity is mapped to a principal by
`policies.yml`, and unknown identities are refused.

Output schemas are pinned: `schemas/mcp/v1/` holds the JSON Schema of every tool's input and
output, and the contract tests fail if the server drifts from them.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Annotated, Any, Literal, TypeVar

from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import BaseModel, ConfigDict, Field

from warehouse_graph.governance.audit import AuditWriter
from warehouse_graph.governance.metadata import filter_graph
from warehouse_graph.governance.policies import (
    PolicyFile,
    Principal,
    resolve,
    resolve_principal,
)
from warehouse_graph.graph.artifact import GraphArtifact
from warehouse_graph.graph.embed import Embedder, get_embedder
from warehouse_graph.graph.query import (
    BlastRadius,
    GraphQuery,
    MetricRecord,
    NodeRecord,
    SearchHit,
    Subgraph,
)
from warehouse_graph.graph.schema import NodeType

SCHEMA_VERSION = "1"
SCHEMA_DIR = Path(__file__).resolve().parents[2] / "schemas" / "mcp" / f"v{SCHEMA_VERSION}"
TOOLS = ("search_nodes", "get_node", "get_lineage", "get_blast_radius", "get_metric")
MAX_K = 50
MAX_DEPTH = 10

INSTRUCTIONS = """Metadata graph of the Kiwi Retail Co. warehouse: dbt sources, models, columns,
metrics, dashboards (exposures), caveats and validated query examples. Results only include what
your role may see. Start with search_nodes to find node ids, then get_node for full records with
caveats. Check `deprecated`, `trap_kind` and caveats before using a table, and prefer canonical
metrics (get_metric shows the canonical alternative)."""

T = TypeVar("T")


class SearchResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    hits: list[SearchHit]


class GraphServer:
    """Holds the graph and one filtered query view per principal."""

    def __init__(
        self,
        graph: GraphArtifact,
        policies: PolicyFile,
        identity: str | None,
        embedder: Embedder | None = None,
        audit: AuditWriter | None = None,
    ) -> None:
        self.graph = graph
        self.policies = policies
        self.spec = resolve(policies, graph)
        self.default_identity = identity
        self.embedder = embedder or get_embedder(graph.meta.embedding_model)
        self.audit = audit
        self._queries: dict[str, GraphQuery] = {}
        if identity is not None:
            resolve_principal(policies, identity)  # fail at startup, not on the first call

    def caller(self) -> Principal:
        token = get_access_token()
        identity = (token.subject or token.client_id) if token else self.default_identity
        if identity is None:
            raise ToolError("no caller identity: authenticate, or start the server with one")
        try:
            return resolve_principal(self.policies, identity)
        except PermissionError as e:
            raise ToolError(str(e)) from None

    def query(self, principal: Principal) -> GraphQuery:
        if principal.name not in self._queries:
            visible = filter_graph(self.graph, self.spec.access[principal.name], self.embedder)
            self._queries[principal.name] = GraphQuery(visible, embedder=self.embedder)
        return self._queries[principal.name]

    def call(self, tool: str, args: dict[str, Any], fn: Callable[[GraphQuery], T]) -> T:
        principal = self.caller()
        try:
            result = fn(self.query(principal))
        except KeyError as e:
            self._audit(principal, "mcp.not_found", {"tool": tool, "args": args})
            raise ToolError(str(e.args[0]) if e.args else "not found") from None
        except ValueError as e:
            raise ToolError(str(e)) from None
        self._audit(principal, "mcp.tool_call", {"tool": tool, "args": args})
        return result

    def _audit(self, principal: Principal, event: str, payload: dict[str, Any]) -> None:
        if self.audit is not None:
            self.audit.write(principal.name, event, payload)


def create_server(
    graph: GraphArtifact,
    policies: PolicyFile,
    identity: str | None = None,
    embedder: Embedder | None = None,
    audit: AuditWriter | None = None,
) -> MCPServer:
    gs = GraphServer(graph, policies, identity, embedder, audit)
    server: MCPServer = MCPServer(
        name="warehouse-graph",
        instructions=INSTRUCTIONS,
        version=f"schema-v{SCHEMA_VERSION}",
    )
    meta = {"schema_version": SCHEMA_VERSION}

    @server.tool(meta=meta)
    def search_nodes(
        query: Annotated[str, Field(description="Free text, e.g. 'net revenue by region'")],
        types: Annotated[
            list[NodeType] | None, Field(description="Only these node types (default: all)")
        ] = None,
        k: Annotated[int, Field(ge=1, le=MAX_K, description="Number of hits")] = 10,
    ) -> SearchResult:
        """Find nodes by hybrid search (BM25 + embeddings). Returns ids, types, flags, scores."""
        return gs.call(
            "search_nodes",
            {"query": query, "types": types, "k": k},
            lambda q: SearchResult(hits=q.search_nodes(query, types, k)),
        )

    @server.tool(meta=meta)
    def get_node(
        id: Annotated[str, Field(description="Node id, e.g. 'model.fct_orders'")],
    ) -> NodeRecord:
        """Full record of one node, with its caveats, columns and direct neighbours."""
        return gs.call("get_node", {"id": id}, lambda q: q.get_node(id))

    @server.tool(meta=meta)
    def get_lineage(
        id: Annotated[str, Field(description="Node id to start from")],
        direction: Literal["upstream", "downstream", "both"] = "upstream",
        depth: Annotated[int, Field(ge=1, le=MAX_DEPTH)] = 3,
        level: Literal["model", "column"] = "model",
    ) -> Subgraph:
        """Upstream/downstream subgraph at model or column level."""
        args = {"id": id, "direction": direction, "depth": depth, "level": level}
        return gs.call("get_lineage", args, lambda q: q.get_lineage(id, direction, depth, level))

    @server.tool(meta=meta)
    def get_blast_radius(
        id: Annotated[str, Field(description="Source, model or column id")],
    ) -> BlastRadius:
        """Everything downstream (models, dashboards, metrics), ranked by dashboard criticality."""
        return gs.call("get_blast_radius", {"id": id}, lambda q: q.get_blast_radius(id))

    @server.tool(meta=meta)
    def get_metric(
        name: Annotated[str, Field(description="Metric name or id, e.g. 'net_revenue'")],
    ) -> MetricRecord:
        """Metric definition, canonical SQL, caveats, and the canonical alternative if any."""
        return gs.call("get_metric", {"name": name}, lambda q: q.get_metric(name))

    return server


async def tool_schemas(server: MCPServer) -> dict[str, dict[str, Any]]:
    """{tool: {"input": schema, "output": schema}} as the server advertises them."""
    tools = await server.list_tools()
    return {
        t.name: {"input": t.input_schema, "output": t.output_schema or {}}
        for t in sorted(tools, key=lambda t: t.name)
    }


def write_schemas(schemas: dict[str, dict[str, Any]], directory: Path = SCHEMA_DIR) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    for name, pair in schemas.items():
        for kind, schema in pair.items():
            path = directory / f"{name}.{kind}.json"
            path.write_text(json.dumps(schema, indent=2, sort_keys=True) + "\n", encoding="utf-8")
