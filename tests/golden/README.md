# Golden sets (version-controlled evaluation assets)

Curated, git-committed fixtures that verify the app behaves to the project
standard. Deterministic/hard-rule checks must be 100%; quality metrics are
thresholded. `known_violation` cases are documented gaps found by a set — they
`xfail` (so the suite stays green) and become `xpass` once fixed.

| Set | File | What it verifies | Runs against |
|---|---|---|---|
| GS1 | `gs1_memory.json` / `test_gs1_memory.py` | Episodic hybrid RRF retrieval — semantic, exact-keyword (BM25, "SYPNL"), recall@k ≥ 0.8 | Qdrant (integration) |
| GS2 | `gs2_entities.json` / `test_gs2_entities.py` | Entity resolution — dominant→resolve, near-collision→disambiguate, no silent wrong-resolution | Qdrant (integration) |
| GS3 | `gs3_behavioral.json` / `test_gs3_behavioral.py` | Behavior gates (§12/§9) — no unprompted disclosure, one-sentence folded disclosure, overclaim rewritten, curiosity gate | in-process (fakes) |
| GS3-judge | `gs3_judge.json` / `test_gs3_judge.py` | LLM-as-judge tone/warmth/length vs the design rubric; pinned model; human-calibrated; negative examples must fail | real LLM (opt-in) |
| GS4 | `gs4_learning.json` / `test_gs4_learning.py` | Learning (§17/§18) — repeated pref crosses threshold, contradiction lowers, correlation candidate-only, no diagnosis | Mongo (integration) |
| GS5 | `gs5_isolation.json` / `test_gs5_isolation.py` | Multi-tenant isolation — ZERO of user A's data reaches user B (absolute) | all user-data stores |

## Running

```bash
docker compose up -d                       # datastores for GS1/GS2/GS4/GS5
uv run pytest tests/golden/                # deterministic + integration sets
RUN_GS3_JUDGE=1 uv run pytest tests/golden/test_gs3_judge.py   # opt-in LLM judge
```

The LLM-as-judge (GS3-judge) is a **regression signal, not ground truth**: its
model is pinned, a human-labeled calibration subset must agree with it before
its scores are trusted, and it is opt-in (network-dependent) so it never
destabilizes the deterministic suite.
