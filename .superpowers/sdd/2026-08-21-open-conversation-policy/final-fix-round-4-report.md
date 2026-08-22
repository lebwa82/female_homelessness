# Final architecture fix report — round 4/5

Base: `5da5c7d`. This round replaced the remaining phrase-, process-, and
surrogate-SQL fixes with explicit clause, transaction, outbox, lease, and
production-repository boundaries. No provider, Telegram, Podman, deployment,
production host, or real PostgreSQL action was performed. All new inputs and
adapter results are synthetic.

## TDD evidence

- The first clause/parser slice was RED at 9 failures and 4 passes. It covered
  punctuation-delimited suicide clauses, dataset version 3, state-bounded
  refusal word order, definite future draft claims, and callback lifecycle
  state.
- Transaction/outbox RED covered rollback after a post-effect outcome failure,
  atomic inbound completion, two-service duplicate processing, callback
  effect/outcome replay, tri-state critical delivery, send recovery without
  diagnostics, NULL follow-up leases, terminal denial, deploy resolution, and
  rollback-bound production assurance.
- Self-review added three independently reproduced RED cases: callback and text
  outcomes sharing one numeric message-id namespace; an unrelated negation
  binding across an entire refusal clause; and assurance omitting follow-up,
  NULL-retention, and tombstone runtime paths. Each is GREEN in the final suite.

## Implemented boundaries

- `deterministic-signals-v3` scans one normalized input into tokens with
  character spans, explicit clause spans, and punctuation/newline boundary
  kinds. Suicide complements are inspected only inside their source clause.
  The version-3 evaluator corpus has 65 cases, including both required
  comma/period regressions and the retained same-clause near misses.
- Workflow refusals use bounded, state-selected object/action stems, negation,
  modal/necessity grammar, and word-order-independent proximity. Callback exits
  use common workflow/reminder cleanup; `followup:same` now renders a state in
  which `more_help` is valid. The draft guard applies clause-local actor,
  referent, inflection, finite/future/passive, and modal/conditional semantics.
- Every inbound service operation enters an identity-serialized unit of work
  before loading state. PostgreSQL acquires a transaction advisory lock, binds
  all production repository calls to one `AsyncSession`, flushes inside that
  transaction, and commits only at the outer boundary. Claim, fresh state,
  effect deduplication, transition, action audit, and durable outcome therefore
  roll back together. The in-memory implementation mirrors this for fault and
  race tests.
- Text and callback events have explicit durable execution namespaces while
  preserving legacy numeric text keys. Outcome persistence atomically completes
  the inbound claim and retains conversation generation, source kind, and
  `critical_delivery`.
- Delivery authorization is `ALLOW`, `DENY_CONFIRMED`, or `UNAVAILABLE` and is
  held through adapter send. Confirmed deletion/generation mismatch denies all
  stale output; storage unavailability fails open only for canonical critical
  output. Initial and replayed critical turns have direct evidence for both
  outage and deletion paths.
- The independent pending-outcome worker leases committed undelivered turns,
  retries adapter failures without diagnostics, reclaims NULL/expired delivery
  leases, acknowledges before optional assistant audit, and sends nothing on a
  second scan.
- Follow-up claims reclaim legacy `processing` rows with NULL leases. Migration
  normalizes those rows; errors release to pending; terminal missing/closed/
  cancelled/generation/chat denials cancel under the locked transaction.
- Production deployment has no environment-controlled privileged PATH or
  executable lookup. Root-consumed commands are fixed absolute paths and their
  path components are verified root-owned and not group/other-writable. Local
  shell-flow tests use a separate non-privileged script-copy harness that the
  production script never reads.
- PostgreSQL assurance runs migrations twice, validates normalized index
  access method/table/ordered columns/uniqueness/predicate, then binds the
  actual production repositories to one rollback-only session. It exercises
  text claim/fail/reclaim/outcome/delivery/ack, legacy NULL follow-up claim/
  release/reclaim, NULL message/contact purge, and comprehensive delete plus
  generation tombstone.
- Live-health and fixture evaluator modes execute the same sequential soft
  create→consume and create→unrelated-expiry conversations and compare the
  actual accumulated user/assistant history at every reported step.

## Self-review rulings

- The only repository `commit` in an inbound unit is the outer transaction
  exit; nested production writes call the shared flush/standalone-commit seam.
  Delivery acknowledgement has its own atomic boundary before optional audit.
- A callback outcome cannot safely reuse a text outcome's numeric key even if
  Telegram usually allocates distinct chat message ids; event kind is part of
  durable identity and legacy text keys remain replayable.
- Exactly two concurrent diagnostic calls and no gateway retry remain the
  contract for every successfully prepared text turn. No diagnostic label
  authorizes deterministic behavior.
- The fixed privileged tool list includes executable paths and their `/`,
  `/usr`, `/usr/local`, and binary-directory ancestors. Test substitution is
  confined to the non-privileged harness copy.

## Fresh verification

| Command | Result |
| --- | --- |
| `UV_OFFLINE=1 UV_CACHE_DIR=/private/tmp/female-homelessness-uv-cache just check` | PASS — Ruff clean; 419 tests passed |
| `UV_OFFLINE=1 UV_CACHE_DIR=/private/tmp/female-homelessness-uv-cache just scenario-smoke` | PASS |
| `UV_OFFLINE=1 UV_CACHE_DIR=/private/tmp/female-homelessness-uv-cache just eval-dialogues` | PASS — 24 focused tests; 65 cases; hard/diagnostic/provider/soft counts all 0 |
| `bash -n scripts/deploy_prod.sh scripts/deploy_prod_test_harness.sh` | PASS |
| `git diff --check` | PASS |

The real PostgreSQL assurance command and deployment gate were intentionally
not run because this round explicitly prohibits real DB, Podman, and deployment
interaction.
