# Task 10 Fix Round 1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close the three Task 10 review gaps without external provider, Podman, or live-evaluation calls.

**Architecture:** The evaluator's versioned fixture schema becomes fully explicit for hard policy evidence, including rule IDs and required canonical copy. PostgreSQL assurance creates and reads a temporary rollback-only historical value. The provider parser rejects ambiguous or non-standard JSON before Pydantic validation.

**Tech Stack:** Python 3.14, pytest, SQLAlchemy async API, JSON fixtures.

**Spec:** `.superpowers/sdd/2026-08-21-open-conversation-policy/task-10-brief.md`

## Global Constraints

- Preserve all 53 dataset histories and never print histories, prompts, replies, secrets, or environment values.
- No provider, live-evaluation, Podman, database, deployment, push, or Telegram operation in this fix round.
- Hard behavior is backend-owned; diagnostics remain non-authoritative.
- Keep exactly two provider calls per text turn and add no provider retry.

---

### Task 1: Make every hard expectation explicit

**Files:**
- Modify: `scripts/dialogue_eval.py`
- Modify: `tests/fixtures/dialogue_scenarios.jsonl`
- Modify: `tests/test_behavior_dataset.py`
- Modify: `tests/test_dialogue_eval.py`

**Interfaces:**
- Consumes: dataset `expected.behavior` objects.
- Produces: hard projections and hashes that include `rule_ids`.

- [x] Write failing loader and replay tests requiring `rule_ids` and canonical copy for canonical backend routes.
- [x] Run focused dataset/evaluator tests and confirm missing expectations fail.
- [x] Add `rule_ids` to the required behavior schema, compare it in `_behavior_failures`, and include it in the hard hash projection.
- [x] Populate all 53 explicit behavior records without altering history entries; preserve null copy only for free open conversation.
- [x] Run focused dataset/evaluator tests and confirm pass.

### Task 2: Verify rollback-only historical escalation storage

**Files:**
- Modify: `scripts/postgres_assurance.py`
- Modify: `tests/test_db_models.py`

**Interfaces:**
- Consumes: SQLAlchemy async connection and transaction.
- Produces: assurance result only after inserting, selecting, and rolling back a temporary historical escalation level.

- [x] Write a failing mocked SQL-order test requiring temporary conversation insert, escalation insert, selected returned level, and rollback.
- [x] Run the focused test and confirm the current read-only assurance fails it.
- [x] Add unique temporary IDs, perform the inserts and value assertion inside the existing transaction, and retain rollback in `finally`.
- [x] Run the focused test and confirm pass.

### Task 3: Reject ambiguous provider JSON

**Files:**
- Modify: `app/agents.py`
- Modify: `tests/test_agents.py`

**Interfaces:**
- Consumes: one provider text response.
- Produces: a mapping only for strict single-object JSON; otherwise an empty mapping that later becomes diagnostic invalid.

- [x] Write failing parser tests for duplicate keys, `NaN`, `Infinity`, and `-Infinity`.
- [x] Run the focused parser test and confirm current parser accepts at least one invalid form.
- [x] Use `json.loads` hooks that reject duplicate keys and non-standard constants, returning no payload on parse failure.
- [x] Run the focused parser test and confirm pass.

### Task 4: Verify and hand off

**Files:**
- Modify: `.superpowers/sdd/2026-08-21-open-conversation-policy/task-10-report.md`

- [x] Run focused tests, `just check`, `just scenario-smoke`, `just eval-dialogues`, and `git diff --check`.
- [x] Update the report with only safe aggregate results and explicit deferred live/PG status.
- [x] Commit the fix round without push or deployment.

## Self-Review

- Task 1 covers hard route evidence and prevents future omission through exact loader keys.
- Task 2 covers both the historical level read and rollback-only transaction ordering.
- Task 3 covers JSON ambiguity at the provider boundary without adding calls or semantic parsing.
- The plan introduces no unscoped operation and contains no placeholder steps.
