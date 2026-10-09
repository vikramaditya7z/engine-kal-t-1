# V1 Autonomous Development Guidelines

## A. Mission and scope

The V1 Goal completes and verifies the market-data boundary of the Kalshi Arbitrage Engine. V1 covers validated normalized market and event data, exact numeric parsing, read-only public REST ingestion, cursor pagination, explicit transport/API errors, offline fixtures, and reproducible verification.

The agent must follow `PROJECT_RULES.md` and `ARCHITECTURE.md`. V1 is one incremental stage of the existing codebase, not a new application.

The Goal must not implement or imply:

- V2 arbitrage detection, opportunity ranking, or optimization;
- order placement, authentication for trading, live trading, or paper execution;
- order-book execution pricing, fees, risk checks, ledgers, replay, or backtesting;
- WebSockets, frontends, agents, microservices, or unrelated refactors.

No market relationship may be inferred from names, tickers, titles, or shared event identifiers. Settlement semantics must remain explicit and unproven relationships must remain unclassified.

## B. Persistent memory protocol

`V1_LOOP_MEMORY.md` is the run-to-run state file for the future Goal. Before inspecting, editing, or executing anything else at the beginning of every run, read it. Then, in this order:

1. Read `V1_LOOP_MEMORY.md`.
2. Read the applicable project instructions, including `PROJECT_RULES.md` and relevant parts of `ARCHITECTURE.md`.
3. Check the repository, working tree, tests, fixtures, and runtime state against memory.
4. Treat repository evidence and actual command output as authoritative when they conflict with an unverified memory claim.
5. Identify the first unfinished task and avoid repeating work already supported by evidence.

Every run must skip work already verified as done, must never repeat a failed approach unchanged, and must record what was done, what worked, what failed and why, and what to do next. Update the memory file at the end of every run, including blocked or interrupted runs.

This document does not create the memory file. If a future Goal starts without it, stop and request that its separately reviewed initial contents be prepared rather than silently inventing history.

At the end of every run, update the memory file with exactly these headings:

### Done

Completed work and the evidence supporting it: files, commands, and actual results.

### Worked

Approaches, interfaces, and checks that worked and must be preserved.

### Failed — don't retry

The failed action, observed reason, and the exact unchanged approach that must not be retried. A retry is allowed only when new evidence, a changed condition, or a materially different approach is recorded first.

### Next

The first concrete, safe action for the next run.

Keep memory short, factual, and actionable. Record partial progress if interrupted. Never record an intended result as a completed result, and never invent test, API, or network outcomes.

## C. Repository inspection and change discipline

Before each implementation task, inspect:

- the relevant source, tests, interfaces, fixtures, and documentation;
- the current working-tree state and any user changes;
- the exact gap and the evidence demonstrating it.

Prefer the smallest justified change. Preserve existing V0 behavior and user work. Do not rewrite working components for style, add speculative abstractions, or modify unrelated files.

Do not commit, push, deploy, place orders, alter external state, or perform other external writes without explicit approval. Do not delete or reset user work. If the repository is not a Git worktree, record that Git diff/status checks are unavailable and use careful file inspection instead.

Preserve the existing baseline commit and history. Never force-push, rewrite history, or use destructive reset or cleanup commands such as `git reset --hard` or `git clean`. Before any push, verify the working tree and remote state; never overwrite unexpected remote changes. Use descriptive, meaningful commits for completed milestones when appropriate. Do not commit secrets, credentials, generated files, or unrelated changes. If a safe Git operation is ambiguous, stop and request human approval.

## D. Modular architecture and future extensibility

Consult `ARCHITECTURE.md` before structural changes. Preserve these boundaries:

`raw Kalshi transport → response validation/normalization → normalized market state → V0 contract/payoff model`

Transport code must not leak raw API dictionaries into domain or future strategy logic. Normalized market/event models own provider-schema validation; contract and portfolio models own settlement mathematics. Future strategy, execution pricing, risk, paper execution, accounting, and replay remain separate responsibilities.

Use explicit interfaces and data contracts, high cohesion, and minimal coupling. Keep deterministic calculations in ordinary code and tests, not in an LLM judgment. Preserve exact `Decimal` handling for normalized API prices and quantities and the existing integer-cent accounting model for V0.

Do not build an abstraction merely because a later version might need it. If a significant design decision is not covered by the current rules, API evidence, or existing interfaces, stop and request approval rather than inventing an architecture.

## E. Verification protocol

Verification is a separate responsibility from implementation. For every run:

1. Establish a baseline before editing.
2. Add a focused regression test for each discovered defect.
3. Run focused tests after the relevant change.
4. Run the complete offline suite before declaring the task complete.
5. Run Python compilation and applicable static checks.
6. Validate `tests/fixtures/kalshi_market_response.json` offline.
7. Run the public smoke test when network access is available:

   ```bash
   PYTHONPATH=. .venv/bin/python scripts/smoke_test_markets.py
   ```

8. Inspect the final diff, or, if Git is unavailable, inspect the complete changed-file list and contents for accidental or unsafe changes.
9. Report exact commands and actual results, distinguishing passed, failed, skipped, blocked, and not-run checks.

The verifier must compare implementation and evidence against the acceptance checklist below; it must not trust an implementation agent’s completion claim. Passing tests alone does not prove that all V1 requirements are implemented.

Classify DNS, proxy, sandbox, API availability, credentials, and similar failures as environment blockers unless evidence identifies a code defect. Do not weaken validation to force a live response through, and do not repeatedly retry an unchanged failure.

## F. V1 acceptance criteria

Every criterion must be marked `PASS`, `FAIL`, `BLOCKED`, or `NOT YET VERIFIED`. No criterion may be marked `PASS` without evidence from the current repository and/or an actual command result.

| Criterion | Required evidence | Current verified status |
|---|---|---|
| Market-data models and normalization | `market_data.py`, focused tests, and validated real fixture | `PASS`: validated models, regression tests, and the refreshed fixture normalize successfully |
| Exact numeric and validation semantics | Decimal/sub-cent/fractional quantity tests; malformed-value rejection | `PASS`: Decimal/sub-cent, fractional quantity, malformed-value, and timestamp regression tests pass |
| Public GET-only REST client | `rest_client.py` and fake-transport tests showing GET-only behavior | `PASS`: covered by `tests/test_rest_client.py` |
| Pagination correctness | Multi-page, empty-cursor, and repeated-cursor tests | `PASS`: covered by offline fake-transport tests |
| HTTP/API error and timeout handling | HTTP, transport, JSON, envelope, identifier, and finite-timeout tests | `PASS`: covered by offline fake-transport tests |
| Real fixture normalization | `tests/fixtures/kalshi_market_response.json` exists and its offline test passes | `PASS`: refreshed live fixture normalizes offline |
| Fresh live smoke compatibility | A current smoke command returns HTTP success, normalizes a market, and reports fixture output | `PASS`: the smoke script returned HTTP 200, normalized a current market, and refreshed the fixture |
| Reproducible verification commands | Commands and actual results recorded in memory/final report | `PASS`: commands and results are recorded in `V1_LOOP_MEMORY.md` |
| Documentation accuracy | Guidelines and project docs match the actual implementation and scope | `PASS`: architecture documentation now distinguishes implemented V0/V1 components from deferred work |
| Architectural modularity and scope control | Final diff review confirms no V2/trading functionality or boundary violations | `PASS`: reviewed V1 diff is limited to validation, tests, fixture, and documentation |

If a requirement is ambiguous, record the ambiguity and request a decision. Do not guess or silently broaden the acceptance criteria. Update the verified status only with new evidence.

## G. Normal completion and stop conditions

The future Goal may declare V1 complete only when:

- every mandatory acceptance criterion is verified;
- required focused tests, the complete offline suite, compilation, and applicable static checks pass;
- a current live smoke test succeeds, with recorded evidence that an actual Kalshi market response was normalized successfully; a fixture test cannot substitute for this evidence;
- the real fixture is validated offline;
- documentation accurately describes the implementation;
- the final diff or changed-file review is complete;
- memory and the final report agree with observed evidence;
- no V2, trading, or other prohibited functionality was introduced.

Completion must distinguish verified completion from completion blocked by the environment. A blocked criterion is never a pass.

If live market-data verification is unavailable or fails because of an environment or API blocker, V1 remains `BLOCKED`, not `COMPLETE`. The agent must pause and report the blocker rather than treating acceptance of the blocker as successful V1 completion.

## H. Emergency and blocker stop conditions

Stop implementation immediately, preserve state, update memory, and report the evidence when:

- a change could compromise financial correctness or settlement semantics;
- existing tests regress and the cause is unclear;
- a proposed fix would weaken validation without a justified API contract change;
- the API schema, optionality, or settlement relationship is ambiguous;
- a structural change requires a decision not covered by the project rules;
- the same approach fails repeatedly without materially different evidence;
- unexpected user changes or a destructive operation is present;
- credentials, private information, or unsafe external actions are involved;
- the environment prevents reliable verification and no safe offline path remains;
- work would exceed V1 scope.

Before stopping, record the partial progress, attempted approaches, exact failure evidence, why continuing is unsafe or unjustified, and the specific decision or external change needed to resume.

## I. Iteration policy and anti-loop safeguards

Each run follows this sequence:

1. Read memory and identify the highest-priority unfinished task.
2. Select one concrete, evidence-backed task.
3. State the expected result and verification method.
4. Implement the smallest justified change.
5. Run focused verification.
6. Review the diff or changed-file contents.
7. Update memory.
8. Continue only if a useful, safe next step remains and no stop condition applies.

Do not make speculative changes merely to keep the loop active. Stop when meaningful progress is unavailable, a human decision is needed, the same failure repeats without a materially different approach, or the Goal budget is exhausted. Budget exhaustion means stop and report; it is not evidence of success.

## J. Final reporting

The final report must include:

- the V1 acceptance checklist with `PASS`, `FAIL`, `BLOCKED`, or `NOT YET VERIFIED` for every item and supporting evidence;
- every changed file and the reason for each change;
- exact commands executed and actual test, compilation, static-check, and smoke-test results;
- live verification status, including environment limitations and whether a real fixture was captured;
- architectural concerns, remaining correctness risks, and unresolved decisions;
- outstanding blockers and the concrete next action;
- explicit confirmation that no V2, order placement, live trading, paper trading, or other prohibited future-version functionality was introduced.
