# Memory

### Done

Repository evidence confirms the V0 contract/portfolio models and V1 market-data normalization, GET-only REST client, smoke script, offline tests, and real fixture at `tests/fixtures/kalshi_market_response.json`. Compilation passed with `PYTHONPYCACHEPREFIX=/tmp/kalshi-pycache .venv/bin/python -m compileall -q kalshi_arbitrage scripts tests` (exit 0). The full suite passed with `.venv/bin/python -m pytest -q`: `115 passed in 0.10s`; the fixture test passed with `.venv/bin/python -m pytest -q tests/test_market_fixture.py`: `1 passed in 0.02s`. V1 is not complete.

The preparation baseline is committed as `dc769a7b412ec966962f5ec2216955594b9863db` (`chore: establish Kalshi project baseline`) on branch `main`. The pre-loop documentation updates were committed as `8a38b8beead6ef57b7fc7bd6f6323540ba274c8c` (`docs: finalize V1 loop safeguards`) and pushed normally to `origin/main`. Remote verification matched the local commit after the push. The working tree was clean after the documentation commit; this memory update is the only subsequent change and will be committed separately. Application source, tests, and fixtures remain unchanged.

### Worked

Existing offline normalization and regression tests are deterministic and exercise the captured fixture. The live public GET returned HTTP 200 with top-level keys `cursor` and `markets`, and one market, without changing the fixture. The `.gitignore` excludes local environments, secrets, caches, logs, databases, and macOS metadata while retaining source, tests, fixtures, and project documents.

### Failed — don't retry

Do not retry the live smoke check unchanged. Its GET succeeded, but normalization rejected the real `updated_time` value `2026-10-09T10:28:39.57017+00:00` with `MarketDataInputError: updated_time must be an ISO-8601 timestamp or null`. Investigate timestamp parsing against current API evidence before any compatibility fix. The existing fixture still normalizes offline; do not fabricate or overwrite it. The first sandboxed `git init` failed with `.git: Operation not permitted`; the approved elevated retry succeeded. Do not retry sandboxed Git metadata writes unchanged.

### Next

Investigate the live `updated_time` compatibility issue using the actual API schema and a focused regression test; then run focused tests, the full suite, compilation, and a fresh live smoke test only after a materially different, evidence-backed change. Keep V1 `BLOCKED` until live normalization succeeds. `ARCHITECTURE.md` contains historical wording inconsistent with the implemented V0/V1 code: it says only project guidance exists and no milestone is implemented. Do not rewrite it silently; reconcile it as an explicit documentation task. Future runs must read this memory first and update all four headings, including blocked runs.
