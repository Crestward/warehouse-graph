"""Checks on the demo warehouse: dbt project shape, planted traps, caveats, query examples.

Manifest-based tests need `demo/artifacts/` (from `make warehouse`) and skip without it.
Query-example tests are `integration`: they run the SQL against local Postgres.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import psycopg
import pytest
import yaml

from warehouse_graph.demo.load import conninfo_from_env

ROOT = Path(__file__).resolve().parents[1]
DEMO = ROOT / "demo"
MANIFEST = DEMO / "artifacts" / "manifest.json"
MODEL_COUNT = 40  # PRD §1.4.1: 40 models, hard cap 50


def _yaml(name: str) -> dict[str, Any]:
    data: dict[str, Any] = yaml.safe_load((DEMO / name).read_text(encoding="utf-8"))
    return data


@pytest.fixture(scope="module")
def manifest() -> dict[str, Any]:
    if not MANIFEST.exists():
        pytest.skip("no demo/artifacts/manifest.json; run `make warehouse`")
    data: dict[str, Any] = json.loads(MANIFEST.read_text(encoding="utf-8"))
    return data


def _models(manifest: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {n["name"]: n for k, n in manifest["nodes"].items() if k.startswith("model.")}


def _node_refs(manifest: dict[str, Any]) -> set[str]:
    refs: set[str] = set()
    for name, node in _models(manifest).items():
        refs.add(f"model.{name}")
        refs.update(f"column.{name}.{c}" for c in node["columns"])
    for s in manifest["sources"].values():
        refs.add(f"source.{s['source_name']}.{s['name']}")
    refs.update(f"metric.{m['name']}" for m in manifest["metrics"].values())
    refs.update(f"exposure.{e['name']}" for e in manifest["exposures"].values())
    return refs


def test_model_count_is_fixed(manifest: dict[str, Any]) -> None:
    assert len(_models(manifest)) == MODEL_COUNT


def test_every_model_has_description_owner_and_tags(manifest: dict[str, Any]) -> None:
    for name, node in _models(manifest).items():
        assert node["description"].strip(), f"{name} has no description"
        assert node["config"]["meta"].get("owner"), f"{name} has no owner"
        assert node["config"]["tags"], f"{name} has no tags"


def test_planted_traps_are_flagged(manifest: dict[str, Any]) -> None:
    models = _models(manifest)
    deprecated = [n for n, m in models.items() if m["config"]["meta"].get("deprecated")]
    assert deprecated == ["orders_v1"]
    assert models["orders_v1"]["config"]["meta"]["replaced_by"] == "fct_orders"
    legacy = models["dim_customers"]["columns"]["legacy_segment"]["config"]["meta"]
    assert legacy["deprecated"] and legacy["replaced_by"]

    kinds = {
        m["config"]["meta"]["trap"]["kind"]
        for m in models.values()
        if "trap" in m["config"]["meta"]
    }
    assert {"misleading_name", "fanout_join", "deprecated_model"} <= kinds

    metrics = {m["name"]: m for m in manifest["metrics"].values()}
    assert metrics["net_revenue"]["config"]["meta"]["canonical"] is True
    assert metrics["revenue"]["config"]["meta"]["canonical"] is False
    assert metrics["churn_rate_marketing"]["config"]["meta"]["superseded_by"] == "churn_rate"

    stale = [s for s in manifest["sources"].values() if s["config"]["meta"].get("stale_on_demand")]
    assert [s["name"] for s in stale] == ["ad_spend"]


def test_exposures_have_criticality(manifest: dict[str, Any]) -> None:
    for e in manifest["exposures"].values():
        assert e["config"]["meta"]["criticality"] in {"high", "medium", "low"}


def test_caveats_point_at_real_nodes(manifest: dict[str, Any]) -> None:
    refs = _node_refs(manifest)
    caveats = _yaml("caveats.yml")["caveats"]
    assert len({c["id"] for c in caveats}) == len(caveats)
    for c in caveats:
        missing = [r for r in c["applies_to"] if r not in refs]
        assert not missing, f"{c['id']} refers to unknown nodes {missing}"


def test_query_examples_file_shape() -> None:
    examples = _yaml("query_examples.yml")["examples"]
    assert len(examples) == 30
    assert len({e["id"] for e in examples}) == 30
    assert len({e["question"].lower() for e in examples}) == 30
    for e in examples:
        assert "salary" not in e["sql"].lower(), e["id"]  # restricted data stays out


@pytest.mark.integration
def test_query_examples_run_and_return_rows() -> None:
    try:
        conn = psycopg.connect(conninfo_from_env())
    except psycopg.OperationalError:
        pytest.skip("local Postgres not reachable; run `make warehouse`")
    with conn, conn.cursor() as cur:
        for e in _yaml("query_examples.yml")["examples"]:
            cur.execute(e["sql"])
            assert cur.fetchall(), f"{e['id']} returned no rows"
