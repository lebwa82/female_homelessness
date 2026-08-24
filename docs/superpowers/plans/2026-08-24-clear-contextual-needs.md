# Clear Context and Contextual Needs Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Keep conversation free-form while adding backend-owned contextual aid buttons and an auditable `/clear` command that resets only provider context.

**Architecture:** The deterministic policy lane derives contextual needs and passes them to the UI renderer without entering an aid workflow. A separate monotonic `context_epoch` on conversations and messages gates provider-bound history while leaving audit history and durable records intact.

**Tech Stack:** Python 3.13, aiogram, Pydantic, SQLAlchemy async, PostgreSQL 18, pytest, uv, just.

**Spec:** `docs/superpowers/specs/2026-08-24-clear-contextual-needs.md`

## Global Constraints

- Telegram only.
- Free conversation remains in `ConversationState.OPEN_CONVERSATION` until a transactional callback is pressed.
- Every ordinary turn ends with the existing `human` choice.
- All choices are produced by backend UI policy; model text cannot define callbacks.
- Show every relevant contextual need choice; there is no count limit.
- `/clear` preserves the conversation id and every durable audit/product record.
- `context_epoch` is separate from deletion/delivery `generation`.
- Existing safety classification and delivery behavior must remain unchanged.
- Use the existing inbound claim/outcome flow for idempotency.

---

### Task 1: Contextual need choices in the open-conversation lane

**Files:**
- Modify: `app/domain.py`
- Modify: `app/signals.py`
- Modify: `app/ui.py`
- Modify: `app/policy.py`
- Modify: `app/service.py`
- Test: `tests/test_signals.py`
- Test: `tests/test_product_scenarios.py`

**Interfaces:**
- Produces: `ChoiceSet.CONTEXTUAL_NEEDS`.
- Produces: `ResolvedTurn.contextual_needs: tuple[NeedKind, ...]`.
- Produces: `choices_for(..., contextual_needs=...)` rendering `need:<kind>` choices followed by `human`.
- Consumes: existing `NeedKind`, open-conversation generation, `need:<kind>` callback workflows, and deterministic signal extraction.

- [ ] **Step 1: Verify the existing RED tests**

Run: `uv run pytest tests/test_signals.py tests/test_product_scenarios.py -q`

Expected: the newly added contextual-choice cases fail because current policy enters `AID_CATALOG`, current callback gating rejects `need:*` in open conversation, and the signal vocabulary misses the new concrete phrases.

- [ ] **Step 2: Implement contextual choices in domain and UI**

Add `ChoiceSet.CONTEXTUAL_NEEDS`, add `contextual_needs` to `ResolvedTurn`, and extend `choices_for` with a keyword-only contextual-needs argument. Map kinds to stable labels: `Помощь с жильём`, `Помощь с едой`, `Помощь с документами`, `Поддержка`, `Помощь для детей`, `Другая помощь`. Preserve first-seen order, remove duplicate kinds, and append the existing `human` choice last.

- [ ] **Step 3: Keep concrete needs in open conversation**

In policy, collect all concrete aid kinds from the deterministic signals, generate the normal open-conversation answer, return `ChoiceSet.CONTEXTUAL_NEEDS`, and set no transactional effect. Do not update workflow fields or create an aid request merely because text was classified.

- [ ] **Step 4: Allow explicit contextual callbacks**

Pass `ResolvedTurn.contextual_needs` through `_render_resolved_turn`. Permit `need:<kind>` callbacks while state is `OPEN_CONVERSATION`; pressing one must call the existing `_handle_need_choice` flow. Preserve the special support callback behavior.

- [ ] **Step 5: Complete safe phrase coverage**

Extend deterministic aid matching for the approved positive phrases and block the paired negated/descriptive forms represented in `tests/test_signals.py`. Do not broaden the safety classifier or copy fixture corpora into prompts or logs.

- [ ] **Step 6: Verify and commit**

Run: `uv run pytest tests/test_signals.py tests/test_product_scenarios.py -q`

Run: `uv run ruff check app/domain.py app/signals.py app/ui.py app/policy.py app/service.py tests/test_signals.py tests/test_product_scenarios.py`

Commit: `feat: add contextual aid suggestions`

---

### Task 2: Auditable `/clear` through context epochs

**Files:**
- Modify: `app/db.py`
- Modify: `app/store.py`
- Modify: `app/service.py`
- Modify: `app/bot.py`
- Test: `tests/test_db_models.py`
- Test: `tests/test_store_audit.py`
- Test: `tests/test_product_scenarios.py`
- Test: `tests/test_bot_adapter.py`

**Interfaces:**
- Produces: `ConversationRecord.context_epoch: int` with default `0`.
- Produces: SQL columns `conversations.context_epoch` and `conversation_messages.context_epoch`, both non-null integer default `0`.
- Produces: `ConversationService.clear(incoming: IncomingMessage) -> AgentTurn`.
- Consumes: existing `claim_text`, `save_text_outcome`, `unit_of_work`, `update`, `history`, `model_history`, and bot `send_turn` paths.

- [ ] **Step 1: Write and run failing tests**

Add tests proving that `/clear` retains the same conversation id, increments only `context_epoch`, clears active workflow fields, preserves messages/aid requests/follow-ups, makes `history` return both epochs, makes `model_history` return only the current epoch, renders the fixed acknowledgement plus `human`, and replays an identical update idempotently.

Run: `uv run pytest tests/test_db_models.py tests/test_store_audit.py tests/test_product_scenarios.py tests/test_bot_adapter.py -q`

Expected: fail because context epochs and the `/clear` command do not exist.

- [ ] **Step 2: Add additive database fields**

Add both ORM fields and idempotent `ALTER TABLE ... ADD COLUMN IF NOT EXISTS` statements in `init_db`. Stamp every new message with the current conversation epoch. Keep `load_history` unfiltered; require the current epoch in `load_model_history`.

- [ ] **Step 3: Implement both stores**

Expose `context_epoch` through `ConversationRecord` and PostgreSQL row mapping. In memory, stamp the epoch in message audit metadata without changing the public tuple shape. Filter only `model_history`. Extend `update` typing to accept integers and ensure an epoch increment participates in the existing transaction/version boundary.

- [ ] **Step 4: Implement idempotent service reset**

Implement `ConversationService.clear` using the same lock, unit-of-work, text claim, replay, action audit, and saved outcome pattern as other commands. Append the command before incrementing the epoch; update state to `OPEN_CONVERSATION`, increment `context_epoch`, and clear the six active workflow fields. Do not touch aid requests, escalation records, follow-up jobs, or `generation`.

- [ ] **Step 5: Register the Telegram command**

Add the aiogram `Command("clear")` handler, construct `IncomingMessage` with text `/clear`, call `conversation_service.clear`, and send through the normal persisted delivery path.

- [ ] **Step 6: Verify and commit**

Run: `uv run pytest tests/test_db_models.py tests/test_store_audit.py tests/test_product_scenarios.py tests/test_bot_adapter.py -q`

Run: `uv run ruff check app/db.py app/store.py app/service.py app/bot.py tests/test_db_models.py tests/test_store_audit.py tests/test_product_scenarios.py tests/test_bot_adapter.py`

Commit: `feat: add auditable context reset`

---

### Task 3: Executable product scenario dataset

**Files:**
- Modify: `tests/fixtures/dialogue_scenarios.jsonl`
- Modify: `tests/test_behavior_dataset.py`
- Modify: `tests/test_dialogue_eval.py`
- Modify: `tests/test_scenario_smoke.py`

**Interfaces:**
- Consumes: public `ConversationService` text, callback, and clear-command behavior.
- Produces: repeatable dataset cases for contextual suggestions, explicit workflow entry, unrelated dialogue, negation, multiple needs, and context reset.

- [ ] **Step 1: Add failing dataset cases**

Add cases with literal expected choice ids, state, record counts, and provider-history boundaries. Every case must identify the observable product regression it catches; do not assert private implementation details or mock-only calls.

- [ ] **Step 2: Run the focused eval suite**

Run: `uv run pytest tests/test_behavior_dataset.py tests/test_dialogue_eval.py tests/test_scenario_smoke.py -q`

Expected before any required harness adjustment: fail only where the dataset runner cannot yet express callback or clear actions.

- [ ] **Step 3: Minimally extend the runner if needed**

Support explicit action types `text`, `callback`, and `clear` using the real service boundary. Record actual turn text, choice ids, conversation state, durable record counts, audit-history length, and provider-history length for assertions.

- [ ] **Step 4: Verify and commit**

Run: `uv run pytest tests/test_behavior_dataset.py tests/test_dialogue_eval.py tests/test_scenario_smoke.py -q`

Run: `uv run ruff check tests/test_behavior_dataset.py tests/test_dialogue_eval.py tests/test_scenario_smoke.py`

Commit: `test: cover contextual dialogue and clear scenarios`

---

### Task 4: Whole-branch verification

**Files:**
- Verify only.

**Interfaces:**
- Consumes: Tasks 1-3.
- Produces: evidence that the feature satisfies the spec without regressions.

- [ ] **Step 1: Run all checks**

Run: `just check`

- [ ] **Step 2: Run the product eval recipe**

Run: `just eval`

- [ ] **Step 3: Inspect the branch diff**

Run: `git diff --check main...HEAD`

Run: `git status --short`

- [ ] **Step 4: Request independent whole-branch review**

Review the complete `main...HEAD` package against `docs/superpowers/specs/2026-08-24-clear-contextual-needs.md`, then fix any load-bearing finding through the bounded review loop.
