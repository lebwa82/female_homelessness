# Systemic Product Contract Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Align red-flag routing, reset semantics, contextual help, and regression evaluation with the approved Telegram MVP contract.

**Architecture:** Qwen produces two concurrent structured diagnostics. Deterministic policy projects them into a product turn, and the service performs only the policy-authorised state/event changes.

**Tech Stack:** Python 3.14, Pydantic, aiogram, OpenAI Responses SDK, PostgreSQL 18, pytest, uv.

**Spec:** `docs/superpowers/specs/2026-08-26-systemic-product-contract-design.md`

## Global Constraints

- No lexical, keyword, or regexp safety classifier.
- Keep model calls concurrent and keep the permanent human button.
- Never make Qwen responsible for callbacks, state, or external actions.
- Every production behaviour change starts with a failing test.

---

### Task 1: Make the model-to-policy safety schema explicit

**Files:**
- Modify: `app/domain.py`, `app/agents.py`, `app/policy.py`
- Test: `tests/test_agents.py`, `tests/test_policy.py`

- [ ] Add failing parser tests for `handoff`, `suicide`, and invalid safety categories.
- [ ] Run the targeted parser tests and observe validation/routing failures.
- [ ] Add closed escalation/category enums and include escalation in `RiskAssessment`.
- [ ] Expand the risk prompt with semantic red-flag examples and the closed JSON contract.
- [ ] Route `handoff` to S11 and `suicide` to S12 independently of urgency.
- [ ] Run targeted agent/policy tests.

### Task 2: Preserve a coherent flow after entry, reset, and escalation

**Files:**
- Modify: `app/service.py`, `app/domain.py`
- Test: `tests/test_product_scenarios.py`, `tests/test_policy.py`

- [ ] Add failing end-to-end tests for `/start` from `CHOOSING_AID`, `/clear`, and S11 continuation with child help.
- [ ] Run the tests and observe stale-workflow/menu failures.
- [ ] Reset ephemeral workflow state to `GREETING` for `/start` and `/clear`.
- [ ] Add a policy effect that enters the classified aid catalogue after S11 continuation, with S03 fallback.
- [ ] Run targeted end-to-end tests.

### Task 3: Turn the product specification into a checked fixture contract

**Files:**
- Create: `tests/fixtures/product_contract.yaml`
- Modify: `tests/test_behavior_dataset.py`, `tests/fixtures/dialogue_scenarios.jsonl`, `tests/fixtures/dialogue_agent_outputs.jsonl`
- Test: `tests/test_product_contract.py`, `tests/test_dialogue_eval.py`

- [ ] Add a failing test that every declared contract scenario has one matching dialogue fixture.
- [ ] Add S01–S19, red-flag, reset, and contextual-button fixture identifiers.
- [ ] Add representative positive and near-miss outputs, including child-custody fears and acute homelessness.
- [ ] Update existing fixture expectations from current accidental behaviour to specified routes.
- [ ] Run the contract and dialogue-evaluation tests.

### Task 4: Verify the complete package

**Files:**
- Modify only files required by earlier tasks.

- [ ] Run `just check`.
- [ ] Run `just eval-dialogues`.
- [ ] Inspect the diff against the specification and confirm every changed scenario has a test.
- [ ] Commit the design, plan, implementation, and tests in one focused commit.
