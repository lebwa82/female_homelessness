# Task 10 — Runtime-faithful acceptance report

## Scope

Implemented the version 2 dialogue evaluator and diagnostic-only fixture boundary. The evaluator preserves all 53 dataset histories, seeds explicit runtime context, and replays the final message through `ConversationService.handle_text()`.

## RED evidence and regression

- Initial evaluator test failed because the old evaluator had no service-path hard projection.
- Service replay exposed `multi-aid-completion-open-01` remaining in `aid_requested` with `replay_workflow` instead of returning to open conversation.
- Added a deterministic explicit-open-conversation signal and policy transition. The regression test now passes; higher-priority safety and human routes continue to win.

## Schema and API changes

- Dataset rows are versioned and provide `initial` runtime context plus separate `behavior` and `diagnostics` expectations.
- Agent fixtures now carry only `safety` and `support` diagnostics with statuses.
- Removed the legacy `SupportPlan`, `SupportAction`, and `AgentEvaluation(risk=..., plan=...)` compatibility boundary.
- Hard hashes include behavioral projection fields only. Reply prose is excluded except for the boolean result of a required canonical-copy check.
- Added `scripts/postgres_assurance.py`; it runs initialization twice and performs only metadata reads plus a rolled-back historical-level read. It requires callback status and lease-expiry scan indexes as well as the callback lease columns; additive migration DDL creates them for existing tables.
- The provider boundary now accepts one text response, strips only a known JSON code fence, and validates exactly one JSON object locally. Non-object or schema-invalid output remains diagnostic-only `invalid`; there is no provider retry. The two-call budget is regression-tested even for invalid diagnostics.
- Safety diagnostic detail fields are defaultable; the local policy continues to receive diagnostic statuses but never takes behavior authority from them.

## Verified local results

| Check | Safe aggregate result |
| --- | --- |
| Focused service/evaluator regression | 2 passed |
| Focused suite | 122 passed |
| `just check` | 248 passed |
| `just scenario-smoke` | passed |
| `just eval-dialogues` | 53 cases; 0 hard failures; 0 diagnostic deltas; 0 provider failures |
| Anonymous health observation A | safety invalid; support completed |
| Anonymous health observation B | safety completed; support completed |
| `git diff --check` | passed before final report addition; rerun before commit |

Offline mutation replay executed expected, wrong-valid, unavailable, and invalid diagnostic variants for every case. All variants retained the expected hard projection.

## PostgreSQL assurance

Blocked locally. The existing Podman machine reported started, but its runtime stopped before the non-destructive `just db-up` connection. `just db-assure` then returned only `postgres_assurance:failed:OSError`. No volume, container, database row, production VM, or deployment was mutated.

## Live gate

The approved final model/service gate passed on two sequential full live evaluations of `35d6b9a`. Both had zero hard and provider failures, with empty provider-failure categories. The single-response JSON boundary retains exactly two calls and no retries.

| Live run | Result |
| --- | --- |
| 1 | 53 cases; 0 hard failures; 23 diagnostic deltas; 8 provider failures |
| 2 | 53 cases; 0 hard failures; 23 diagnostic deltas; 9 provider failures |
| 3 | 53 cases; 0 hard failures; 22 diagnostic deltas; 9 provider failures |
| 4 (final 1) | 53 cases; 0 hard failures; 27 diagnostic deltas; 0 provider failures; failure categories empty |
| 5 (final 2) | 53 cases; 0 hard failures; 26 diagnostic deltas; 0 provider failures; failure categories empty |

No case output, histories, prompts, response IDs, or provider text were retained from these approved live runs. PostgreSQL assurance remains a separate blocked release gate.

## Fix round 1 — hardening acceptance evidence

- TDD evidence added for all three review findings. The dataset now requires an explicit exact `rule_ids` array for every case; it is compared as a hard behavior field and remains part of the hard projection hash. All 53 fixture expectations were populated without changing case IDs or history values; verified open paths retain explicit empty arrays, while 29 backend-owned routes carry non-null canonical-copy expectations.
- Dataset loading rejects omitted or malformed `rule_ids`, and rejects missing canonical-copy expectations for any backend-owned effect or choice. Expectations remain literal fixture data: the evaluator does not synthesize them at runtime.
- The provider JSON boundary now rejects duplicate object keys and the non-standard constants `NaN`, `Infinity`, and `-Infinity`; malformed diagnostics remain fail-safe and no extra provider request is added.
- PostgreSQL assurance now creates a unique temporary conversation and a `human_requested` escalation within the existing rollback-only transaction, reads back its own stored level, verifies it, and rolls the transaction back. Mocked SQL-order coverage proves no committed row or cleanup delete is used.
- This fix round made no provider, live-evaluation, Podman, or real-PostgreSQL calls. The offline service replay aggregate was 53 cases, with 0 hard failures, 0 diagnostic deltas, and 0 provider failures.

## Fix round 2 — safe live-failure classification

- Two approved full live evaluations of `48bee95` completed before this local-only fix: run 1 had 53 cases, 0 hard failures, 23 diagnostic deltas, and 8 provider failures; run 2 had 53 cases, 0 hard failures, 23 diagnostic deltas, and 9 provider failures. No case output, histories, prompts, response IDs, or provider text were retained.
- The evaluator now projects provider failures through a strict allow-list: agent (`safety` or `support`), diagnostic status (`invalid` or `unavailable`), transport error class, Pydantic validation field/type, and one fixed output-envelope category. Per-case metadata and the CLI summary discard every other audit field, including values, input hashes, response IDs, model names, token usage, and error origins.
- The live summary now aggregates counts by agent, diagnostic status, transport error type, validation field/type, and output-envelope category. It leaves hard behavior, history replay, two calls per text turn, and no-retry policy unchanged.
- The existing 12-second provider client timeout was reviewed and made explicit through a tested client factory; SDK retries and agent retries remain zero. Evaluator concurrency remains capped at four.
- Local verification after this change: 257 tests passed, scenario smoke passed, and offline replay was 53 cases with 0 hard failures, 0 diagnostic deltas, and 0 provider failures.
- No provider, live-evaluation, Podman, or real-PostgreSQL call was made in this fix round. An empty isolated package cache made one blocked dependency-metadata DNS attempt before tests; it fetched nothing, and all subsequent verification used the already-installed environment or `UV_OFFLINE=1`.

## Fix round 3 — narrow partial diagnostic normalization

- Approved live run 3 produced 53 cases, 0 hard failures, 22 diagnostic deltas, and 9 provider failures. Its safe aggregate classified 4 safety and 5 support failures; all were invalid JSON-object outputs, with 0 transport failures. Validation categories were rationale `string_too_long` (4), intent `enum` (5), and need hint `enum` (2).
- Safety now truncates only a string rationale longer than 240 characters and records `safety_rationale_truncated`. Support clears only unknown string `intent` and `need_hint` values to `None`, recording the corresponding finite categories. No unknown value is converted to a real enum, and valid draft text/suggested support retain their existing soft-only semantics.
- Missing or invalid safety level, missing or invalid support draft text, non-string enum data, and invalid suggested support remain invalid. `intent=None` is treated as no soft authority and recorded safely in evaluator diagnostic deltas.
- This fix round made no provider, live-evaluation, Podman, or real-PostgreSQL call.

## Self-review

- Service evaluator, explicit workflow state, mutation invariance, stable order, bounded case concurrency, and output redaction are covered by tests.
- No histories, reply prose, prompts, environment values, credentials, proxy data, Telegram data, or database records appear in this report or evaluator output.
- The live model/service gate passed. Release remains deferred because local PostgreSQL assurance is blocked on the Podman runtime. No deployment, push, or Telegram action was taken.

## Commit

Created as the local commit `Evaluate production policy behavior end to end`; the handoff records its final SHA. This follow-up records the passed final live gate without changing the deferred PostgreSQL release condition.
