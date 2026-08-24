# Model-only routing design

## Goal

Replace all backend lexical matching with structured Qwen diagnostics.  Qwen is
the sole source for risk classification, support intent, and contextual aid
categories.  The backend owns only schema validation, persistence, callback
handling, and state transitions triggered by an already rendered button.

## Decision

Each user text turn continues to make exactly two parallel Qwen calls:

- `risk` returns `level`, `categories`, `confidence`, and an audit-safe rationale;
- `support` returns a conversational draft, `intent`, optional
  `suggested_support`, and `need_hints`, a deduplicated ordered list of
  `NeedKind` values.

There is no local keyword, regular-expression, token, stemming, or lexical
policy layer.  Model diagnostics are authoritative for their respective
domains.  The permanent `human` button remains independent of classification.

## Model contract

`SafetyDiagnostic` remains the structured risk contract.  Its completed
diagnostic is projected into the persisted `RiskAssessment` with detector
`model-risk`.

`SupportDiagnostic.need_hint` is replaced by
`need_hints: tuple[NeedKind, ...]`.  The support prompt asks for every relevant
need in order of usefulness and explicitly permits an empty list.  It continues
to prohibit model-generated callback IDs, workflow state, claims that an
external action has already happened, or application-side effects.

## Authoritative policy projection

For a completed diagnostic pair, `ConversationPolicy` applies these rules in
order:

1. `risk.level == critical` produces the existing critical copy and escalation
   event.
2. `support.intent == explicit_human_request` produces the existing human
   handoff event.
3. An active callback-created workflow is replayed or completed from its state;
   free text is not reclassified into a different workflow while contact or
   location collection is active.
4. `support.intent == psychologist_request` starts contact collection.
5. `support.intent == aid_interest` opens the broad need-category keyboard.
6. A pending psychologist offer plus `psychologist_considering` renders the
   existing psychologist-interest button.
7. All remaining turns render the model draft and every `need_hints` item as a
   contextual aid button, followed by the permanent human button.

Clicking a contextual aid button begins the existing deterministic aid catalog
workflow.  A model category never itself creates an aid request, saves a
contact, or sends an escalation to a specialist.

## Error behavior

The product assumption is that both models are available.  Runtime still
handles transport, JSON, or schema failure safely: no inferred classification
is made, the bot returns its neutral fallback, and the permanent human button
remains.  There is no local fallback matcher.

## Code changes

- Delete `app/signals.py` and `app/safety.py`.
- Remove `HardSignalKind`, `SignalMatch`, `DeterministicSignals`, matcher
  metadata, and their database audit plumbing.
- Replace local-risk and signal inputs in `PolicyContext` with the completed
  model diagnostics and their statuses.
- Remove lexical draft guards from `app/policy.py`; model output is accepted
  after schema validation and length limits.
- Simplify `ConversationService.handle_text` to persist the inbound text,
  evaluate both diagnostics, persist their audits, project model risk, and
  resolve exactly one policy turn.
- Keep callback, contact, aid-catalog, retention, PII-redaction, and database
  behavior unchanged.

## Tests and acceptance criteria

- Natural language describing a lost, stolen, or otherwise urgent document
  problem yields the model-provided legal aid button without any local phrase
  table.
- Multiple model categories produce every corresponding aid button plus the
  permanent human button.
- A model critical risk yields the existing crisis response and escalation
  event.
- A model human-request intent yields the existing handoff event.
- With invalid or unavailable diagnostics, the reply contains only the safe
  fallback and human button; no local text classification occurs.
- No import or repository file references the deleted matcher modules or
  matcher audit/version fields.
- Existing callback-created workflows, data retention, PII redaction, and
  Postgres audit persistence remain covered by the full test suite.

## Non-goals

- This change does not change aid inventory, contact collection, Telegram
  transport, database schema for conversation messages, or deployment.
- It does not deploy or push automatically; those are separate operator
  actions after the implementation is verified.
