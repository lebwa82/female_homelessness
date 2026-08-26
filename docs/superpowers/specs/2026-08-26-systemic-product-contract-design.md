# Systemic Product Contract Design

## Goal

Make the Telegram MVP follow the product specification predictably: Qwen recognises the meaning of a message, while backend policy owns escalation, state, buttons, and durable events.

## Source of truth

The baseline is `chatbot_spec_nevidimiy_fond.md`, supplemented by agreed MVP decisions:

- Telegram only; Chatwoot is a future adapter.
- Human escalation is simulated by an event in PostgreSQL; the woman can keep writing.
- Qwen is the sole semantic classifier; there is no keyword or regexp matcher.
- Every turn has the live-human button. All relevant contextual need buttons may be shown; there is no two-button cap.
- `/start` and `/clear` begin the explicit S01 → S03 path.
- In a red-flag situation, S11 is shown first. On continuing, the bot offers the classified relevant help; if there is none, it shows S03.

## Architecture

```
message → Qwen risk diagnostic ─┐
                                ├→ deterministic policy → state + text + buttons + escalation event
message → Qwen support diagnostic┘
```

The two Qwen calls remain concurrent. Qwen never emits callback identifiers, state transitions, or actions.

### Structured risk contract

`SafetyDiagnostic` gains:

- `escalation: none | handoff | suicide` — product route, independent of urgency;
- `categories` as a closed enum taxonomy, including `violence_threat`, `acute_homelessness`, `child_safety`, and `emotional_crisis`.

Policy rules:

1. `suicide` always renders S12 and records a safety escalation.
2. `handoff` always renders S11 and records a safety escalation, regardless of `urgency`.
3. `critical` with no valid route remains fail-safe S11, preserving compatibility with existing model outputs.
4. A support-side explicit request for a person remains an S13-style simulated handoff.

On S11 continuation, policy creates an ordinary post-escalation help turn: the classified need becomes an aid catalogue; otherwise it enters S03. The original crisis event remains recorded.

### State-entry contract

`/start` and `/clear` clear only ephemeral workflow fields and set `GREETING`; neither deletes the transcript. `continue` from `GREETING` always enters S03. `/delete` remains the explicit irreversible data-delete command.

### UI contract

Policy emits symbolic `ChoiceSet`s. UI renders their backend-owned buttons and appends the permanent live-human button. Copy that promises a menu is used only with the matching `ChoiceSet`; open Qwen text remains conversational and does not own flows.

### Contract and evaluations

`product_contract.yaml` maps every scenario and cross-cutting rule to fixture IDs. A contract test fails if a scenario lacks a fixture. Separate tests cover policy projection from model output, in-memory end-to-end state transitions, and an opt-in live Qwen evaluation command. Live output is judged by structured fields and rendered route, not exact supportive prose.

## Explicit MVP gaps

Chatwoot notifications, coordinator schedule/S14, and a confirmed external `DELIVER` event remain deferred. Follow-up is currently scheduled after a simulated completed aid request. Raw-transcript retention versus the original privacy section requires a separate product decision because the later product decision authorised local full-message storage.
