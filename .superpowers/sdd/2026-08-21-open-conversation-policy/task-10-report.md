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

The first healthcheck did not export variables from the sourced environment and was not a valid provider check. With safe auto-export, a provider output could be structurally non-actionable under the prior prompted-output boundary. The single-response JSON boundary removes retries while retaining exactly two calls. The observed health results are not consistently green: one run had safety invalid/support completed, while the following anonymized observation had both completed. No additional retry was made.

The two full-history live evaluator runs are pending a fresh approval-flow authorization. The escalation review rejected the first requested run before execution because it would export the 53 histories to the external provider, and specifically required a renewed approval after the risk warning. This task does not retry or circumvent that decision.

| Live run | Result |
| --- | --- |
| 1 | not run — fresh full-history external approval pending |
| 2 | not run — fresh full-history external approval pending |

## Fix round 1 — hardening acceptance evidence

- TDD evidence added for all three review findings. The dataset now requires an explicit exact `rule_ids` array for every case; it is compared as a hard behavior field and remains part of the hard projection hash. All 53 fixture expectations were populated without changing case IDs or history values; verified open paths retain explicit empty arrays, while 29 backend-owned routes carry non-null canonical-copy expectations.
- Dataset loading rejects omitted or malformed `rule_ids`, and rejects missing canonical-copy expectations for any backend-owned effect or choice. Expectations remain literal fixture data: the evaluator does not synthesize them at runtime.
- The provider JSON boundary now rejects duplicate object keys and the non-standard constants `NaN`, `Infinity`, and `-Infinity`; malformed diagnostics remain fail-safe and no extra provider request is added.
- PostgreSQL assurance now creates a unique temporary conversation and a `human_requested` escalation within the existing rollback-only transaction, reads back its own stored level, verifies it, and rolls the transaction back. Mocked SQL-order coverage proves no committed row or cleanup delete is used.
- This fix round made no provider, live-evaluation, Podman, or real-PostgreSQL calls. The offline service replay aggregate was 53 cases, with 0 hard failures, 0 diagnostic deltas, and 0 provider failures.

## Self-review

- Service evaluator, explicit workflow state, mutation invariance, stable order, bounded case concurrency, and output redaction are covered by tests.
- No histories, reply prose, prompts, environment values, credentials, proxy data, Telegram data, or database records appear in this report or evaluator output.
- Task remains blocked on the local Podman runtime, a consistently green provider-health gate, and the approval-flow authorization for full-history live evaluation. No deployment, push, or Telegram action was taken.

## Commit

Created as the local commit `Evaluate production policy behavior end to end`; the handoff records its final SHA.
