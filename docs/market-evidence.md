# Market evidence (task 0.6)

Purpose: confirm the Azure + Databricks choice (PRD §6.1) against **ads I would actually apply for**, not a general sample. If the result leaned AWS, PRD §6.6–6.7 would be swapped to AWS equivalents **at the same size** (Lambda + SQS + Secrets Manager); nothing else would change.

## Finding (2026-10-26)
I reviewed current NZ AI/ML engineering ads I'd apply for. Almost all of them state the stack the same way:

> **Stack:** Cloud-native engineering on **Azure, AWS, or GCP**; **Python**; and **modern MLOps tooling**.

The ads weren't logged one by one: they are close to identical on stack, so a per-ad table would add nothing.

| Signal | What it means for the project |
|---|---|
| Cloud is "Azure, AWS **or** GCP" | Employers want depth on one major cloud, not a particular one. There's no lean towards AWS, so the swap trigger doesn't fire. |
| Python | Already the only language in the project (PRD §7). |
| "Modern MLOps tooling" | Covered by what v1 already builds (see the mapping below). There's nothing to add. |

## How v1 already shows "modern MLOps tooling"
Use this mapping in the READMEs, the CV and interviews. It isn't new scope.

| MLOps practice employers mean | Where it's shown in v1 |
|---|---|
| CI/CD with automated quality gates | GitHub Actions on all repos; the eval regression gate on PRs labelled `eval` |
| Evaluation as a release gate | P1 Gate A / Gate B and the P2 eval harness with a held-out test split |
| Model training, versioning, reproducibility | System 1 LightGBM trained on 150 offline bundles; fixed seeds; pinned dependencies (`uv.lock`) and git-tag pins between repos |
| Infrastructure as code | Terraform for the Azure profile; OIDC `plan` in CI |
| Monitoring and observability | App Insights; audit trail; `costs.csv` ledger and `docs/cost.md` |
| Cost and budget controls on LLM calls | `LLMClient` budget cap, response cache, Batch API, prompt caching |
| Secrets management | Key Vault in `azure`; `.env` git-ignored locally |
| Governance | `policies.yml` compiled to Postgres RLS and Unity Catalog row filters / masks |

## Decision
- [x] **Confirms Azure + Databricks: no change.** Ads accept any of the three clouds. Azure still has the strongest signal in the NZ data-platform sample (PRD §6.1), and Azure Databricks + Unity Catalog fits the governance design. Recorded as D7 in `design-decisions.md`.
- [ ] Leans AWS: swap PRD §6.6–6.7 to Lambda + SQS + Secrets Manager (same size)
