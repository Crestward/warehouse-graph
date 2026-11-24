"""The metadata graph: dbt artifacts + caveats + query examples -> one versioned file.

build:  DbtArtifacts (MetadataSource) -> artifact.build() -> GraphArtifact.save()
query:  GraphArtifact.load() -> GraphQuery (search_nodes, get_node, get_lineage, ...)
"""

from warehouse_graph.graph.artifact import GraphArtifact, build
from warehouse_graph.graph.config import GraphConfig
from warehouse_graph.graph.dbt import DbtArtifacts, MetadataSource
from warehouse_graph.graph.query import GraphQuery

__all__ = ["DbtArtifacts", "GraphArtifact", "GraphConfig", "GraphQuery", "MetadataSource", "build"]
