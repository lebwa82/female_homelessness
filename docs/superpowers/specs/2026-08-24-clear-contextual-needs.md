# Clear Context and Contextual Needs Specification

## Product intent

The Telegram conversation remains an open, model-led dialogue. Transactional aid workflows begin only after the person explicitly presses a relevant button. Backend policy, not the model response, owns which buttons are rendered.

## Contextual need suggestions

- Every ordinary bot reply contains `human` as the final choice.
- A free-text message may add any number of relevant `need:<kind>` choices before `human`.
- Supported kinds are the existing `NeedKind` values: housing, food/money, legal, support, children, and other.
- Concrete first-person requests add matching choices. Descriptive, negated, hypothetical, and unrelated text must not add them.
- Detecting a need does not change `ConversationState.OPEN_CONVERSATION`, create an aid request, or set transactional pending fields.
- The dialogue response remains a normal empathetic answer generated through the existing open-conversation lane.
- Pressing `need:<kind>` from `OPEN_CONVERSATION` starts the existing workflow for that kind.
- There is no maximum number of relevant need choices.
- The model never invents callback identifiers or button labels.

## `/clear`

- `/clear` keeps the existing conversation row and identity.
- It preserves every stored message, event, agent run, aid request, escalation record, and follow-up job for audit.
- It increments a separate `context_epoch`; it must not reuse deletion/delivery `generation`.
- Provider-bound model history includes only messages from the conversation's current `context_epoch`.
- Audit history continues to return all retained messages from all epochs.
- It resets the active conversational workflow to `OPEN_CONVERSATION` and clears `need`, `pending_aid_id`, `pending_contact_method`, `pending_city`, `pending_district`, and `pending_offer`.
- It returns `Контекст очищен. Можно начать заново — я рядом.` with only the `human` choice.
- Retried delivery of the same Telegram update is idempotent through the existing inbound claim/outcome mechanism.

## Verification dataset

The executable scenario dataset must cover:

- one concrete need and its optional button;
- several needs and all corresponding buttons;
- negated and descriptive mentions with no need button;
- unrelated conversation with only `human`;
- clicking a contextual button starts the existing workflow;
- `/clear` hides prior turns from provider history without deleting audit history or durable product records.
