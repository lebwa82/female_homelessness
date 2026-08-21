# Task 9 report — diagnostic agents and deterministic policy

## Scope

- Base: `c89d196`.
- Worktree: `/Users/lebwa82/female_homelessness/.worktrees/open-conversation-policy`.
- Scope correction approved by the coordinator: the brief named a nonexistent
  `tests/test_service_scenarios.py`; the existing
  `tests/test_product_scenarios.py` was migrated instead. Its obsolete
  model-owned action expectations were replaced with `PolicyContext` behavior.
- A later coordinator direction authorized the minimal Task-10-facing
  compile/runtime adapter in `scripts/dialogue_eval.py`; it now supplies a
  `PolicyContext` from final-user deterministic signals and fixture diagnostics.
  It does not replay service state or allow fixture model fields to choose an
  effect. Dataset schema and DB schema were not changed.
- No environment, live-model, deploy, Telegram, paid, or database-schema
  operation was performed.

## RED evidence

1. The Task 9-focused baseline passed: `uv run pytest tests/test_agents.py
   tests/test_signals.py tests/test_policy.py tests/test_ui.py
   tests/test_product_scenarios.py -q` → `149 passed`.
2. Contract tests were added before production changes. The RED run of the same
   focused command stopped in collection with the expected missing policy
   capability: `AttributeError: type object 'PolicyEffect' has no attribute
   'START_NEED_DISCOVERY'`.

## API and ownership migration

- Added `DiagnosticStatus`, `SafetyDiagnostic`, `SupportDiagnostic`, and
  `PolicyContext`. `SupportDiagnostic` has only diagnostic intent, optional
  need/evidence hints, guarded `draft_text`, and the soft psychologist marker.
- Converted the gateway to exactly two concurrent `create_task` calls plus one
  `gather`: safety and support. It records `completed|invalid|unavailable`
  separately from local risk; it has no repair or retry call.
- Made `rationale` canonical and accepts `rationale_short` only as a one-way
  provider alias. Alias use, evidence claim count/validation/hash, provider
  settings, and PII summaries are audit-safe; raw rationale and evidence text
  are not recorded. The normalized live typed-output shape is covered, so its
  alias audit flag cannot cause a parser `KeyError`.
- Extended deterministic signals with bounded psychologist pending-offer
  acknowledgements/questions. Exact `да, хочу` is the only bare acceptance;
  an unrelated reply clears the marker instead of authorizing a later workflow.
- Replaced `resolve_turn(risk, plan, state)` in the production text path with
  `resolve_turn(PolicyContext)`. The kernel alone owns effects, side effects,
  workflow replay, generic need discovery, catalog selection, and contextual
  choice sets. Model labels/need hints do not authorize them.
- Routed every text message through that kernel once and the existing executor
  once. Callback IDs, store calls, and idempotency mechanics remain unchanged.
- Moved the permanent human affordance into `app.ui.py`: contextual
  `ChoiceSet.NONE` is empty, while final rendering always appends the stable
  `human` callback.
- Versioned the policy audit (`deterministic-policy-v2`) with a strict
  structured allow-list. It includes state, local risk, diagnostic labels/statuses,
  matcher/policy versions, rule IDs, choices/callbacks, effects, side effects,
  and fallback; it excludes text, history, prompts, evidence quotes, and
  `next_action`.
- Updated the design contract with diagnostic-only ownership, canonical
  rationale/alias behavior, pending-offer flow, deterministic precedence, and
  global human affordance.
- Strengthened the guarded conversational copy against false completed-action
  claims, including accepted requests and promised callbacks.
- Restored product coverage for aid/location/contact workflows, follow-up
  completion, stale callbacks, and retries before/after idempotent handoff
  effects; no test relies on a model action field.
- Removed public `AgentEvaluation.risk`/`.plan` action projections. Offline
  legacy fixture inputs are converted only at the boundary; smoke and health
  helpers now create diagnostic evaluations directly.
- Migrated the evaluator call site from the removed three-argument resolver to
  `PolicyContext`. It deliberately surfaces seven legacy fixture route deltas
  rather than weakening deterministic policy to make model-owned expectations
  pass.

## Verification

- `uv run pytest tests/test_agents.py tests/test_policy.py tests/test_signals.py
  tests/test_product_scenarios.py tests/test_dialogue_eval.py -q` → `136 passed`.
- `just check` → `214 passed` (ruff and full pytest).
- `just scenario-smoke` → passed: aid, open conversation, psychologist request,
  and crisis paths.
- `git diff --check` → passed.

## Self-review

- Verified local critical precedes human; human precedes local inspection
  failure and finite workflow replay.
- Verified concern/urgent adds `RECORD_SAFETY` alongside compatible local routes;
  critical canonical hotline copy does not depend on model output.
- Verified wrong, missing, malformed, and unavailable model diagnostics cannot
  alter hard local route effects, contextual choices, or side effects.
- Verified all 53 fixture final user turns receive a deterministic route; open
  and human-near-miss rows have no contextual menu or handoff.
- Verified the exact `выговориться/выслушать` regression renders only the
  permanent human callback, with no escalation or generic need menu.

## Deferred Task 10 work

The adapter intentionally stops before Task 10 acceptance work. Task 10 must
version the fixture payload/dataset contract to diagnostic schemas, persist or
reconstruct actual workflow/pending context, and run a real service-path replay.
It must then replace the seven recorded legacy model-owned route expectations
with approved deterministic expectations. The Task 9 adapter does not infer an
effect, choice set, or workflow from a fixture model intent/plan.

## Commit

`Move product actions into deterministic policy` (created after the fresh
`just check`, scenario smoke, diff check, and read-only review recorded above).
