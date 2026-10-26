"""Node and edge types of the metadata graph (PRD §2.2).

Node ids are short, readable and stable, because they are what an LLM sees and quotes back:

    source.<source>.<table>      model.<name>          column.<model>.<column>
    column.<source>.<table>.<column>                    metric.<name>
    exposure.<name>              caveat.<id>           query_example.<id>

They match the reference format of `demo/caveats.yml`. `Incident` / `AFFECTED` are reserved
in the PRD but parked (§1.4.3), so they are not defined here.
"""

from __future__ import annotations

import datetime as dt
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter


class NodeType(StrEnum):
    SOURCE = "source"
    MODEL = "model"
    COLUMN = "column"
    METRIC = "metric"
    EXPOSURE = "exposure"
    CAVEAT = "caveat"
    QUERY_EXAMPLE = "query_example"


class EdgeType(StrEnum):
    DEPENDS_ON = "DEPENDS_ON"  # model -> model | source
    HAS_COLUMN = "HAS_COLUMN"  # model | source -> column
    COLUMN_DERIVES_FROM = "COLUMN_DERIVES_FROM"  # column -> upstream column
    DEFINES = "DEFINES"  # metric -> model (and -> metric, for ratio inputs)
    EXPOSES = "EXPOSES"  # exposure -> model
    HAS_CAVEAT = "HAS_CAVEAT"  # any node -> caveat
    ANSWERED_BY = "ANSWERED_BY"  # model -> query example that reads it


Criticality = Literal["high", "medium", "low"]
Severity = Literal["high", "medium", "low"]


class Trap(BaseModel):
    """A planted trap (docs/planted-traps.md), from `meta.trap`."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: str
    note: str = ""
    use_instead: str | None = None


class _Node(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str
    name: str
    description: str = ""
    owner: str | None = None
    tags: tuple[str, ...] = ()
    deprecated: bool = False
    replaced_by: str | None = None  # node id
    trap: Trap | None = None


class SourceNode(_Node):
    type: Literal[NodeType.SOURCE] = NodeType.SOURCE
    source_name: str
    relation: str
    loader: str | None = None
    loaded_at_field: str | None = None
    freshness_warn_hours: float | None = None
    freshness_error_hours: float | None = None
    stale_on_demand: bool = False
    sensitivity: str | None = None
    # From sources.json (the last `dbt source freshness`): pass | warn | error | runtime error.
    freshness_status: str | None = None
    max_loaded_at: dt.datetime | None = None


class ModelNode(_Node):
    type: Literal[NodeType.MODEL] = NodeType.MODEL
    layer: str | None = None
    materialization: str | None = None
    relation: str
    contains_pii: bool = False
    sensitivity: str | None = None
    # From run_results.json (the last `dbt build`).
    last_run_status: str | None = None
    last_successful_run: dt.datetime | None = None
    compiled_sql: str = ""


class ColumnNode(_Node):
    type: Literal[NodeType.COLUMN] = NodeType.COLUMN
    parent: str  # node id of the model or source
    data_type: str | None = None
    tests: tuple[str, ...] = ()
    pii: str | None = None  # name | email | phone | salary
    sensitivity: str | None = None  # restricted | pii
    masked: bool = False  # set only in a principal's filtered view of the graph


class MetricNode(_Node):
    type: Literal[NodeType.METRIC] = NodeType.METRIC
    label: str = ""
    metric_type: str  # simple | ratio
    canonical: bool = False
    helper: bool = False  # building block for a ratio metric, not answered directly
    superseded_by: str | None = None  # node id
    team: str | None = None
    model: str | None = None  # node id of the model the measures read
    time_dimension: str | None = None
    grain: str | None = None
    filters: tuple[str, ...] = ()
    expression: str = ""  # e.g. "sum(net_revenue_nzd)" or "metric.a / metric.b"
    sql: str = ""  # canonical SQL, total over all time; add a where/group by on time_dimension


class ExposureNode(_Node):
    type: Literal[NodeType.EXPOSURE] = NodeType.EXPOSURE
    label: str = ""
    exposure_type: str = ""
    criticality: Criticality | None = None
    maturity: str | None = None
    url: str | None = None


class CaveatNode(_Node):
    """`description` holds the caveat text."""

    type: Literal[NodeType.CAVEAT] = NodeType.CAVEAT
    severity: Severity
    author: str | None = None
    added_on: dt.date | None = None


class QueryExampleNode(_Node):
    """`description` holds the natural-language question."""

    type: Literal[NodeType.QUERY_EXAMPLE] = NodeType.QUERY_EXAMPLE
    sql: str
    notes: str | None = None
    author: str | None = None
    added_on: dt.date | None = None


Node = Annotated[
    SourceNode | ModelNode | ColumnNode | MetricNode | ExposureNode | CaveatNode | QueryExampleNode,
    Field(discriminator="type"),
]
NODE_ADAPTER: TypeAdapter[Node] = TypeAdapter(Node)


class Edge(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    source: str
    target: str
    type: EdgeType


CRITICALITY_RANK: dict[str | None, int] = {"high": 3, "medium": 2, "low": 1, None: 0}


def column_id(parent_id: str, column: str) -> str:
    """`model.fct_orders` + `order_id` -> `column.fct_orders.order_id`."""
    return f"column.{parent_id.split('.', 1)[1]}.{column}"
