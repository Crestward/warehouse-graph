"""`policies.yml` -> principals and per-principal access, resolved against the graph (3.2-3.3).

`load_policies` validates the file. `resolve` turns its selectors (`pii:email`, ...) into
concrete relations and columns using the graph's tags, giving a `PolicySpec`. That one spec
feeds both the Postgres compiler and the metadata filter, so what an LLM can see and what the
database lets it query come from the same decision.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field

from warehouse_graph.graph.artifact import GraphArtifact
from warehouse_graph.graph.schema import ColumnNode, ModelNode, SourceNode

POLICY_FILE = Path("policies.yml")
VIEW_SCHEMAS = {"marts"}  # served through policy views, so masking is possible here
_RELATION = re.compile(r'^"?([^".]+)"?\."?([^".]+)"?\."?([^".]+)"?$')


class RowFilter(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    column: str
    equals: str
    unfiltered: tuple[str, ...] = ()  # tables without the column that are still allowed


class PrincipalPolicy(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    description: str
    schemas: tuple[str, ...]
    row_filter: RowFilter | None = None
    hidden: tuple[str, ...] = ()
    masked: tuple[str, ...] = ()


class PolicyFile(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    version: int = Field(ge=1, le=1)
    masks: dict[str, str]
    principals: dict[str, PrincipalPolicy]
    identities: dict[str, str]
    audit_readers: tuple[str, ...] = ()


@dataclass(frozen=True)
class Principal:
    """A demo role (or the `sentinel` service principal) and the login role it maps to."""

    name: str
    description: str


def load_policies(path: Path = POLICY_FILE) -> PolicyFile:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    policies = PolicyFile.model_validate(data)
    for identity, principal in policies.identities.items():
        if principal not in policies.principals:
            raise ValueError(f"identity {identity} maps to unknown principal {principal}")
    for reader in policies.audit_readers:
        if reader not in policies.principals:
            raise ValueError(f"audit reader {reader} is not a principal")
    for name, p in policies.principals.items():
        if not re.fullmatch(r"[a-z][a-z0-9_]{0,62}", name):
            raise ValueError(f"principal name {name!r} is not a safe database role name")
        if p.row_filter and set(p.schemas) - VIEW_SCHEMAS:
            # Row filters are RLS policies on the tables behind the marts views.
            raise ValueError(f"principal {name}: a row filter needs schemas within {VIEW_SCHEMAS}")
    return policies


def resolve_principal(policies: PolicyFile, identity: str) -> Principal:
    """Map a request identity to its principal. Unknown identities are refused."""
    name = policies.identities.get(identity)
    if name is None:
        raise PermissionError(f"identity {identity!r} is not mapped to any principal")
    return Principal(name=name, description=policies.principals[name].description)


def relation_parts(relation: str) -> tuple[str, str, str]:
    """`"warehouse"."marts"."fct_orders"` -> ("warehouse", "marts", "fct_orders")."""
    m = _RELATION.match(relation)
    if not m:
        raise ValueError(f"can't parse relation {relation}")
    return m.group(1), m.group(2), m.group(3)


@dataclass(frozen=True)
class Relation:
    node_id: str
    schema: str
    table: str
    columns: tuple[str, ...]  # in warehouse order
    kind: str  # model | source
    materialization: str | None


@dataclass
class Access:
    """What one principal may see and query."""

    principal: Principal
    relations: dict[str, Relation] = field(default_factory=dict)  # visible, by node id
    hidden_columns: set[str] = field(default_factory=set)  # column node ids
    masked_columns: dict[str, str] = field(default_factory=dict)  # column node id -> mask
    row_filter: RowFilter | None = None

    def visible_columns(self, relation: Relation) -> list[str]:
        return [
            c
            for c in relation.columns
            if f"column.{relation.node_id.split('.', 1)[1]}.{c}" not in self.hidden_columns
        ]


@dataclass
class PolicySpec:
    """`policies.yml` resolved against one graph: the input to every policy compiler."""

    policies: PolicyFile
    relations: dict[str, Relation]  # every model/source relation in the graph
    access: dict[str, Access]  # by principal name

    def principal(self, name: str) -> Principal:
        return self.access[name].principal


def _relations(graph: GraphArtifact) -> dict[str, Relation]:
    columns: dict[str, list[str]] = {}
    for n in graph.nodes.values():
        if isinstance(n, ColumnNode):
            columns.setdefault(n.parent, []).append(n.name)
    out: dict[str, Relation] = {}
    for n in graph.nodes.values():
        if isinstance(n, ModelNode | SourceNode):
            _, schema, table = relation_parts(n.relation)
            out[n.id] = Relation(
                node_id=n.id,
                schema=schema,
                table=table,
                columns=tuple(columns.get(n.id, [])),
                kind=n.type.value,
                materialization=n.materialization if isinstance(n, ModelNode) else None,
            )
    return out


def _matches(selector: str, col: ColumnNode, short_parent: str) -> bool:
    kind, _, value = selector.partition(":")
    if value and kind == "pii":
        return col.pii is not None and (value == "*" or col.pii == value)
    if value and kind == "sensitivity":
        return col.sensitivity == value
    if not value and "." in selector:
        return selector == f"{short_parent}.{col.name}"
    raise ValueError(f"bad column selector {selector!r}")


def resolve(policies: PolicyFile, graph: GraphArtifact) -> PolicySpec:
    relations = _relations(graph)
    columns = [n for n in graph.nodes.values() if isinstance(n, ColumnNode)]
    spec = PolicySpec(policies=policies, relations=relations, access={})

    for name, p in policies.principals.items():
        access = Access(Principal(name, p.description), row_filter=p.row_filter)
        for rel in relations.values():
            if rel.schema not in p.schemas:
                continue
            rf = p.row_filter
            if rf and rf.column not in rel.columns and rel.table not in rf.unfiltered:
                continue  # can't be filtered, so it isn't shown (deny by default)
            access.relations[rel.node_id] = rel
        for selector in [*p.hidden, *p.masked]:  # catch typos: every selector must match
            if not any(_matches(selector, c, c.parent.split(".", 1)[1]) for c in columns):
                raise ValueError(f"principal {name}: selector {selector!r} matches no column")

        for col in columns:
            owner = access.relations.get(col.parent)
            if owner is None:
                continue
            short = col.parent.split(".", 1)[1]
            if any(_matches(s, col, short) for s in p.hidden):
                access.hidden_columns.add(col.id)
            elif any(_matches(s, col, short) for s in p.masked):
                mask = policies.masks.get(col.pii or "default", policies.masks.get("default"))
                if mask is None:
                    raise ValueError(f"principal {name}: no mask for {col.id} (pii={col.pii})")
                if owner.schema in VIEW_SCHEMAS:
                    access.masked_columns[col.id] = mask
                else:
                    access.hidden_columns.add(col.id)  # no masking view here: fail closed
        spec.access[name] = access
    return spec
