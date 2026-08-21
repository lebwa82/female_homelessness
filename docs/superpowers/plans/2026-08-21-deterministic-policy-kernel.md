# Deterministic Policy Kernel Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make buttons, workflow transitions, safety escalation, and handoff reproducible even when the two Yandex model diagnostics vary.

**Architecture:** Keep exactly two concurrent model calls for safety and support, but treat their structured outputs as diagnostics and conversational drafts. A versioned local token matcher produces auditable high-precision signals; a deterministic policy kernel alone authorizes effects, contextual buttons, state changes, and persisted side effects. Live evaluation must replay the same service path as Telegram and separate deploy-blocking behavioral failures from model-diagnostic drift.

**Tech Stack:** Python 3.14, Pydantic 2, aiogram 3, pydantic-ai/OpenAI Responses, PostgreSQL 18, pytest, uv, just.

**Spec:** `docs/superpowers/specs/2026-08-21-open-conversation-policy-design.md`

## Global Constraints

- Telegram remains the only channel in this MVP; keep channel-neutral domain/store boundaries for future Chatwoot.
- Every user turn still starts exactly two Yandex calls concurrently: SafetyAgent and SupportAgent.
- Open conversation is the default: no contextual menu when no verified transactional signal exists; the permanent human button remains available.
- “Выслушай / хочу выговориться / поговори со мной” is conversation, not handoff. Explicit transfer to a person or explicit refusal to speak to a bot is handoff.
- Critical self-harm copy retains `8-800-2000-122`; verified current danger has highest precedence.
- Model diagnostics never authorize callbacks, effects, state transitions, requests, or escalations.
- Do not log raw prompts, history, evidence quotes, secrets, or environment values. Audit only hashes, versions, rule IDs, typed statuses, and resolved fields.
- Do not weaken or delete the 53 anonymized dialogue histories to make live evaluation pass.
- Use TDD for every behavior change and commit each task independently.

---

### Task 8: Versioned deterministic signal extractor

**Files:**
- Create: `app/signals.py`
- Create: `tests/test_signals.py`
- Modify: `app/domain.py`
- Modify: `app/safety.py`
- Test: `tests/test_safety.py`

**Interfaces:**
- Produces `extract_signals(text: str) -> DeterministicSignals`.
- Produces backend-only `HardSignalKind`, `SignalMatch`, and `DeterministicSignals` domain types.
- `SignalMatch` contains only `kind`, stable `rule_id`, token offsets, and optional `need`; audit must never contain matched text.
- Replaces broad local safety regex ownership with bounded token-sequence rules.

- [ ] **Step 1: Write failing positive and near-miss matcher tests**

  Cover every explicit-human fixture and the four near misses, punctuation/case/`ё`, token boundaries, negation, concrete need categories, generic aid interest, psychologist tentative/explicit acceptance, suicide/self-harm now, violence now, no shelter tonight/current eviction, and concern without immediacy. Assert stable rule IDs and no raw text in `model_dump()`.

- [ ] **Step 2: Run focused tests and confirm RED**

  Run: `uv run pytest tests/test_signals.py tests/test_safety.py -q`

- [ ] **Step 3: Implement deterministic normalization and bounded token grammar**

  Use Unicode NFKC, casefold, `ё→е`, a small character scanner, and explicit token sequences with bounded optional words. Do not use semantic embeddings, stemming, or broad `.*` regexes. A human signal requires transfer/connect plus an external human role, or explicit rejection of the bot; conversational “human” wording alone must not match.

- [ ] **Step 4: Replace broad safety patterns with signal-derived local assessment**

  Critical and urgent local assessment must derive only from reviewed rule IDs. Model/schema status must not be ranked as a semantic local risk.

- [ ] **Step 5: Run focused and full tests**

  Run: `uv run pytest tests/test_signals.py tests/test_safety.py -q`

  Run: `just check`

- [ ] **Step 6: Commit**

  Commit message: `Add deterministic conversation signals`

---

### Task 9: Diagnostic-only agents and deterministic policy ownership

**Files:**
- Modify: `app/domain.py`
- Modify: `app/agents.py`
- Modify: `app/policy.py`
- Modify: `app/service.py`
- Modify: `app/ui.py`
- Modify: `tests/test_agents.py`
- Modify: `tests/test_policy.py`
- Modify: `tests/test_service_scenarios.py`
- Modify: `tests/test_ui.py`
- Modify: `docs/superpowers/specs/2026-08-21-open-conversation-policy-design.md`

**Interfaces:**
- Model-facing outputs become `SafetyDiagnostic` and `SupportDiagnostic`; support exposes diagnostic intent, optional need hint/evidence claims, and `draft_text`, but no action, choice set, catalog item IDs, callback IDs, workflow state, or effect.
- `AgentEvaluation` exposes diagnostic payload/status/audit for both calls.
- `resolve_turn(context: PolicyContext) -> ResolvedTurn` consumes state, deterministic signals, diagnostic statuses, diagnostics, pending offer, and backend catalog.
- `ConversationService.handle_text()` always extracts signals, awaits the same two concurrent calls, then invokes the kernel once.

- [ ] **Step 1: Write failing schema and gateway tests**

  Assert exactly two concurrent calls remain. Validate evidence against the latest current-user span; invalid evidence is diagnostic-only and cannot authorize behavior. Canonical risk field is `rationale`; `rationale_short` is a one-way provider alias whose use is recorded without storing its text. Derive audit sampling settings from the same settings object passed to the provider.

- [ ] **Step 2: Write the failing policy truth table**

  Cover critical×human, human×invalid safety schema, human in finite workflows, unknown-local-input×aid, urgent/concern×aid/open/human, malformed/missing support, psychologist tentative/explicit, generic aid interest, active workflow×new workflow. Expected precedence:

  1. verified critical;
  2. verified explicit human;
  3. deterministic local inspection failure;
  4. active finite workflow;
  5. verified concern/urgent side effect plus compatible route;
  6. verified psychologist request;
  7. verified concrete need;
  8. verified generic aid interest;
  9. verified close;
  10. open conversation.

- [ ] **Step 3: Write model-mutation invariance tests**

  For every hard corpus route, replace diagnostics with wrong-but-valid enums, missing support, invalid safety, and contradictory need hints. Assert the hard projection—effect, side effects, contextual choice set, callbacks, and state transition—does not change for the same local signals and workflow state.

- [ ] **Step 4: Implement diagnostic schemas and gateway boundary**

  SafetyAgent receives the current redacted user turn plus minimal typed context; SupportAgent receives history/catalog/knowledge. Transport/schema failure is `DiagnosticStatus.INVALID` or `UNAVAILABLE`, not semantic `RiskLevel.UNKNOWN`. Do not add retries or a third call.

- [ ] **Step 5: Implement `PolicyContext` and policy kernel**

  Effects and contextual choices must be derived from verified signals and state only. Model `draft_text` may be used solely for open conversation after an output guard rejects claims that a person was called, a request was saved, or a workflow started; otherwise use canonical safe copy. The permanent human affordance is rendered by the backend independently from contextual buttons.

- [ ] **Step 6: Route the service through the kernel and one executor**

  Preserve callback IDs, DB records, idempotency keys, escalation causes, and historical rows. Remove post-policy corrections and all model-owned action/menu fields from runtime.

- [ ] **Step 7: Run focused and full tests**

  Run: `uv run pytest tests/test_agents.py tests/test_policy.py tests/test_service_scenarios.py tests/test_ui.py -q`

  Run: `just check`

  Run: `just scenario-smoke`

- [ ] **Step 8: Update design contract and commit**

  Document `rationale` as canonical, the compatibility alias, diagnostic versus behavioral ownership, and explicit global human affordance. Commit message: `Move product actions into deterministic policy`

---

### Task 10: Runtime-faithful behavioral acceptance

**Files:**
- Modify: `tests/fixtures/dialogue_scenarios.jsonl`
- Modify: `scripts/dialogue_eval.py`
- Modify: `tests/test_dialogue_eval.py`
- Modify: `justfile`
- Modify: `README.md`
- Test: `tests/test_dialogue_eval.py`

**Interfaces:**
- Dataset rows carry enough initial state/pending-offer context to replay the production service path.
- Evaluator replays histories through `ConversationService` with an in-memory store and the real signal/policy/UI path.
- Report has `hard_failures` and `diagnostic_deltas`; CLI exits nonzero only for hard behavioral failure or invalid dataset/provider failure.
- Hard projection includes safety route, effect/side effects, contextual choices, rendered callbacks, state, escalation cause/count, request count, and mandatory crisis copy.

- [ ] **Step 1: Write a failing test proving the old evaluator bypasses service behavior**

  Use psychologist pending-offer and active-workflow cases where direct `resolve_turn()` and service replay differ. Require the evaluator to assert final rendered callbacks/state/effects.

- [ ] **Step 2: Version dataset state and expectations without weakening histories**

  Preserve all 53 histories. Move model `risk`/`intent` expectations into diagnostic expectations. Keep or strengthen behavioral expectations. Correct generic `aid-08` to backend `need_categories` because no concrete need is known; do not let the model invent a category.

- [ ] **Step 3: Implement service-path replay and two-channel report**

  Offline fixtures may vary diagnostic fields while hard projections remain stable. CLI output must include only case IDs, typed classifications, rule IDs, hashes, and failure names—never histories/prompts/quotes/secrets.

- [ ] **Step 4: Add deterministic mutation and repeated-live tests**

  Run every case against at least three diagnostic mutations and assert identical hard projections. Live mode must report diagnostic drift separately and require zero hard failures in two sequential full runs.

- [ ] **Step 5: Run local acceptance**

  Run: `just check`

  Run: `just scenario-smoke`

  Run: `just eval-dialogues`

  Run: `git diff --check`

- [ ] **Step 6: Run safe integration acceptance**

  Without printing `.env`, source the existing project environment and run `just llm-health`, then two sequential `just eval-dialogues-live` runs. Start existing PostgreSQL only through the documented non-destructive command; run schema initialization twice and verify required columns/indexes/historic enum reads. If local Podman remains unavailable, perform the same non-destructive check on the existing deployment VM after confirming the target service and volume; do not remove/recreate volumes.

- [ ] **Step 7: Commit**

  Commit message: `Evaluate production policy behavior end to end`

---

## Final review and release gate

- [ ] Generate one review package from branch base through Task 10 and dispatch an independent full-branch reviewer.
- [ ] Resolve all Critical/Important findings with one focused fixer and scoped re-review.
- [ ] Run `just check`, `just scenario-smoke`, `just eval-dialogues`, `just llm-health`, and two sequential live evals on the reviewed commit.
- [ ] Deploy only if hard failures are zero in both live runs and PostgreSQL migration assurance passes.
- [ ] Verify the deployed build metadata and service health, then push the integrated commit to GitHub.
