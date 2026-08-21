# Task 10 Fix Round 3 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Normalize only the observed non-authoritative diagnostic defects so live diagnostics remain observable and safe without a retry or semantic enum substitution.

**Architecture:** The provider boundary will locally normalize a long string safety rationale and unknown string support enum labels before Pydantic validation. Normalization records only finite categories. `SupportDiagnostic.intent` becomes optional, and service/audit/evaluator paths will treat `None` as no soft intent while retaining a completed diagnostic and explicit normalization delta.

**Tech Stack:** Python 3.14, Pydantic v2, dataclasses, pytest.

**Spec:** Parent-approved Task 10 fix round 3 instruction (2026-08-22).

## Global Constraints

- No provider, live-evaluation, Podman, or real-PostgreSQL calls.
- Normalize only: safety string rationale over 240 characters; unknown string support `intent`; unknown string support `need_hint`.
- Never map an unknown value to a real enum, infer an action, retry, or retain a normalized source value in audit/output.
- Missing/invalid safety `level`, missing/invalid support `draft_text`, invalid `suggested_support`, and non-string enum payloads remain invalid.
- Preserve exactly two concurrent calls, zero SDK/agent retries, the explicit 12-second timeout, max evaluator concurrency four, and hard behavior/history hashes.

---

### Task 1: Provider-boundary partial normalization

**Files:**
- Modify: `app/agents.py:329-395`
- Modify: `app/domain.py:157-166`
- Test: `tests/test_agents.py`

**Interfaces:**
- Consumes: `AgentCallResult.payload` and audit dictionaries.
- Produces: completed `SafetyDiagnostic` or `SupportDiagnostic` with only safe optional values plus `normalization: {"categories": [...]}` from a finite allow-list.

- [x] **Step 1: Write failing parser tests for all allowed normalizations and invalid boundaries.**

```python
assert safety.rationale == "x" * 240
assert safety_audit["normalization"] == {"categories": ["safety_rationale_truncated"]}
assert support.intent is None and support.need_hint is None
assert support_audit["normalization"] == {"categories": [
    "support_unknown_intent_cleared", "support_unknown_need_hint_cleared"
]}
assert missing_draft_status is DiagnosticStatus.INVALID
assert invalid_level_status is DiagnosticStatus.INVALID
```

- [x] **Step 2: Run the parser tests and verify the existing strict validation returns invalid.**

Run: `.venv/bin/python -m pytest tests/test_agents.py::<new-test-names> -q`

- [x] **Step 3: Make `SupportDiagnostic.intent` optional and add narrowly typed pre-validation normalization.**

```python
if isinstance(value, str) and value not in {item.value for item in SupportIntent}:
    payload["intent"] = None
    categories.add("support_unknown_intent_cleared")
```

- [x] **Step 4: Re-run focused agent tests and verify no retry/call-budget regression.**

Run: `.venv/bin/python -m pytest tests/test_agents.py -q`

### Task 2: Service and evaluator observability

**Files:**
- Modify: `app/service.py:456-485`
- Modify: `scripts/dialogue_eval.py:606-645`
- Test: `tests/test_dialogue_eval.py`, `tests/test_product_scenarios.py`

**Interfaces:**
- Consumes: optional support intent and finite normalization audit categories persisted by `ConversationService`.
- Produces: policy audit with `support_intent=None`, diagnostic projection categories, and safe normalization deltas while hard behavior stays unchanged.

- [x] **Step 1: Write failing service/evaluator tests.**

```python
assert policy_audit["support_intent"] is None
assert report.diagnostics["support_normalizations"] == (
    "support_unknown_intent_cleared", "support_unknown_need_hint_cleared"
)
assert report.diagnostic_deltas == (
    "support_intent:normalized_unknown",
    "support_normalization:support_unknown_need_hint_cleared",
)
assert report.hard_failures == ()
```

- [x] **Step 2: Run the tests and verify current service audit dereferences a missing optional intent or evaluator omits normalization information.**

Run: `.venv/bin/python -m pytest tests/test_dialogue_eval.py::<new-test-name> tests/test_product_scenarios.py::<new-test-name> -q`

- [x] **Step 3: Guard service audit access and add evaluator projection/delta mapping only for the three finite normalization categories.**

- [x] **Step 4: Re-run focused evaluator and product tests.**

Run: `.venv/bin/python -m pytest tests/test_dialogue_eval.py tests/test_product_scenarios.py -q`

### Task 3: Report, verification, and commit

**Files:**
- Modify: `.superpowers/sdd/2026-08-21-open-conversation-policy/task-10-report.md`
- Modify: `docs/superpowers/plans/2026-08-22-task-10-fix-round-3.md`

- [x] **Step 1: Add approved live run 3 aggregate: 53 cases, 0 hard failures, 22 diagnostic deltas, 9 provider failures; state this fix round is local-only.**
- [x] **Step 2: Run focused suites, `UV_OFFLINE=1 just check`, `UV_OFFLINE=1 just scenario-smoke`, offline evaluator with aggregate-only summary, and `git diff --check`.**
- [x] **Step 3: Obtain a read-only review and make a separate local commit without push or deployment.**
