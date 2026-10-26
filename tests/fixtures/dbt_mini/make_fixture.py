"""Regenerate the mini dbt fixture by trimming the real demo artifacts.

    uv run python tests/fixtures/dbt_mini/make_fixture.py   (after `make warehouse`)

Keeps a small, realistic slice: the deprecated orders_v1 chain (and its replacement), the
marketing spend chain up to an exposure and a metric, and the HR chain with a restricted
salary column. Dependencies on models outside the slice are dropped, so the fixture is
self-contained.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[3]
SRC = ROOT / "demo" / "artifacts"
OUT = Path(__file__).resolve().parent
PKG = "kiwi_retail"

SOURCES = [
    "shop.orders",
    "shop.payments",
    "marketing.ad_spend",
    "marketing.campaigns",
    "hr.employees",
]
MODELS = [
    "stg_shop__orders",
    "stg_shop__payments",
    "orders_v1",
    "fct_orders",
    "stg_marketing__ad_spend",
    "stg_marketing__campaigns",
    "int_marketing__spend_daily",
    "fct_ad_spend_daily",
    "stg_hr__employees",
]
EXPOSURES = ["marketing_performance_dashboard"]
METRICS = ["ad_spend_nzd"]
SEMANTIC_MODELS = ["ad_spend"]
DROP = ("raw_code", "created_at", "unrendered_config", "docs", "patch_path", "build_path")


def _load(name: str) -> dict[str, Any]:
    data: dict[str, Any] = json.loads((SRC / name).read_text(encoding="utf-8"))
    return data


def _trim(node: dict[str, Any], keep: set[str]) -> dict[str, Any]:
    node = {k: v for k, v in node.items() if k not in DROP}
    deps = node.get("depends_on")
    if deps:
        node["depends_on"] = {"macros": [], "nodes": [d for d in deps["nodes"] if d in keep]}
    return node


def main() -> None:
    m = _load("manifest.json")
    keep = {f"source.{PKG}.{s}" for s in SOURCES} | {f"model.{PKG}.{n}" for n in MODELS}
    keep |= {f"metric.{PKG}.{n}" for n in METRICS} | {f"exposure.{PKG}.{n}" for n in EXPOSURES}
    tests = {
        uid: n
        for uid, n in m["nodes"].items()
        if n["resource_type"] == "test" and n.get("attached_node") in keep
    }
    manifest = {
        "metadata": m["metadata"],
        "nodes": {u: _trim(n, keep) for u, n in m["nodes"].items() if u in keep}
        | {u: _trim(n, keep) for u, n in tests.items()},
        "sources": {u: _trim(n, keep) for u, n in m["sources"].items() if u in keep},
        "metrics": {u: _trim(n, keep) for u, n in m["metrics"].items() if u in keep},
        "exposures": {u: _trim(n, keep) for u, n in m["exposures"].items() if u in keep},
        "semantic_models": {
            u: n for u, n in m["semantic_models"].items() if n["name"] in SEMANTIC_MODELS
        },
    }
    c = _load("catalog.json")
    catalog = {
        "metadata": c["metadata"],
        "nodes": {u: n for u, n in c["nodes"].items() if u in keep},
        "sources": {u: n for u, n in c["sources"].items() if u in keep},
        "errors": None,
    }
    r = _load("run_results.json")
    run_results = {
        "metadata": r["metadata"],
        "results": [
            {k: v for k, v in x.items() if k not in ("compiled_code", "adapter_response")}
            for x in r["results"]
            if x["unique_id"] in keep
        ],
    }
    s = _load("sources.json")
    sources = {
        "metadata": s["metadata"],
        "results": [x for x in s["results"] if x["unique_id"] in keep],
    }
    for name, data in [
        ("manifest.json", manifest),
        ("catalog.json", catalog),
        ("run_results.json", run_results),
        ("sources.json", sources),
    ]:
        (OUT / name).write_text(json.dumps(data, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    print(f"wrote {OUT}: {len(keep)} nodes, {len(tests)} tests")


if __name__ == "__main__":
    main()
