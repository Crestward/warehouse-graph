# Parking lot

Good ideas that are deliberately **not** part of v1 (PRD §1.4.3). The rule is to write one line here and get back to the current task. Nothing on this list is reconsidered before Phase 17 (packaging) is done. The README's "next steps" section is drawn from this file.

| Idea | Why not in v1 | Added |
|---|---|---|
| Public benchmarks: Spider 2.0-Lite, BIRD, AMBROSIA/TrustSQL, Loghub-2.0, Spider2-DBT | They have no dbt manifest, so they need a separate schema-introspection graph and adapter, and they show the least of what's different here (lineage, caveats, governance) | 2026-10-22 |
| Incident memory (`record_incident` / `search_incidents`, incident store, chronological memory experiment) | Needs its own leakage-safe experiment; not needed for the graph-value claim (B3) | 2026-10-22 |
| Observed lineage from Unity Catalog system tables + lineage drift report | The synthetic demo has no consumers outside dbt, so it would show nothing new | 2026-10-22 |
| Retriever 3 (graph-only) and its ablation | Hybrid vs. vector-only carries the main claim | 2026-10-22 |
| P2 model sweep; `compound` fault class; more than 150 eval incidents | Cost and time; no change to the core claims | 2026-10-22 |
| Claude in Microsoft Foundry as an `LLMProvider` | Paid subscription only, US/Sweden regions; the Anthropic API is the default | 2026-10-22 |
| Service Bus / Container Apps / KEDA / ACR worker architecture | Functions + a storage queue give the same buffering, retries and scale-to-zero with fewer resources | 2026-10-22 |
| Cheap open-weights LLM hosted with acceptable data residency | Third-party cheap APIs were evaluated and rejected (PRD §5.4) | 2026-10-22 |
| System One decision models (TypeSafe Jev, Liquid d1-ultra, Microsoft-Decision-1, open H2O-Lightning-4B via Ollaya) as an alternative System 1 tier, through one `/v1/systemone` adapter, compared with LightGBM on accuracy, calibration and $/incident | v1 System 1 must be free, run offline and be rerunnable by reviewers; the category is weeks old and Jev is waitlisted. Docs and interview prep only | 2026-10-23 |
| MLflow experiment tracking / model registry for the System 1 LightGBM model (free in Databricks) | Ads ask for "modern MLOps tooling", but v1 already shows CI/eval gates, IaC, monitoring and cost controls; adding MLflow would break swap-never-add | 2026-10-26 |
