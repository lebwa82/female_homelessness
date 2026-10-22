from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

import pytest
from test_chatwoot_service import StubGateway, ordinary_evaluation

from app.catalog import CERTIFICATE_AID_IDS
from app.chatwoot.certificates import CertificateInventory
from app.domain import IncomingMessage, PolicyEffect, ResolvedTurn
from app.scenario import AIDS, SCREENS, render
from app.service import ConversationService, available_catalog
from app.store import CertificateClaimResult, InMemoryConversationStore, StoredCertificate

PAUSED_TEXT = (
    "За последние сутки мы получили больше обращений, чем можем обработать и пока что, "
    "к сожалению, сертификаты недоступны. Из того, что можно выбрать прямо сейчас:"
)
CONSULTATION_LABELS = (
    "Консультация психолога",
    "Консультация юриста",
    "Разговор с консультанткой по теме зависимости/созависимости",
)


def incoming(message_id: int, text: str = "") -> IncomingMessage:
    return IncomingMessage(
        channel="chatwoot", platform_user_id=7, chat_id=23,
        message_id=message_id, text=text,
    )


@pytest.fixture
async def flow():
    store = InMemoryConversationStore()
    service = ConversationService(
        store=store, gateway=StubGateway(ordinary_evaluation()), html_scenario=True,
    )
    await service.start(incoming(1, "/start"))
    record = await store.get(incoming(1))
    return service, record, store


@pytest.mark.parametrize("html_scenario,selected_certificate", (
    (False, False), (True, False), (True, True),
))
async def test_model_context_respects_new_certificate_pause(
    monkeypatch, html_scenario, selected_certificate,
):
    store = InMemoryConversationStore()
    gateway = StubGateway(ordinary_evaluation())
    contexts = []

    async def evaluate(context):
        contexts.append(context)
        return gateway.result

    monkeypatch.setattr(gateway, "evaluate", evaluate)
    service = ConversationService(store=store, gateway=gateway, html_scenario=html_scenario)
    await service.start(incoming(1, "/start"))
    if selected_certificate:
        await service.handle_callback(incoming(2), "continue")
        await service.handle_callback(incoming(3), "need:food_money")
        paused = await service.handle_callback(incoming(4), "aid:food_card")
        assert paused.text == PAUSED_TEXT

    await service.handle_text(incoming(5, "Расскажите о доступной помощи."))

    assert len(contexts) == 1
    context = contexts[0]
    assert context.catalog == tuple(
        item.model_dump(mode="json") for item in available_catalog()
        if not html_scenario or item.fulfillment != "certificate"
    )
    certificate_ids = {item["id"] for item in context.catalog if item["fulfillment"] == "certificate"}
    assert certificate_ids == (set() if html_scenario else CERTIFICATE_AID_IDS)
    assert context.new_certificates_unavailable is html_scenario
    assert not store.aid_requests


@pytest.mark.parametrize("need,aid_id", (
    ("food_money", "food_card"), ("food_money", "medicine_card"),
    ("housing", "hostel_3_nights"), ("children", "children_card"),
))
async def test_new_certificate_request_shows_only_available_consultations(flow, need, aid_id):
    service, record, store = flow
    await service.handle_callback(incoming(2), "continue")
    await service.handle_callback(incoming(3), f"need:{need}")
    turn = await service.handle_callback(incoming(4), f"aid:{aid_id}")

    assert turn.text == PAUSED_TEXT
    assert tuple(choice.label for choice in turn.choices
                 if choice.id != "human" and not choice.id.startswith("back:")) == CONSULTATION_LABELS
    assert any(choice.id == "human" for choice in turn.choices)
    assert any(choice.id.startswith("back:") for choice in turn.choices)
    assert record.pending_aid_id is None
    assert not store.aid_requests
    assert not any(action[3].get("kind") == "certificate_waitlist" for action in store.actions)

    replay = await service.handle_callback(incoming(4), f"aid:{aid_id}")
    assert replay == turn
    stale_confirm = await service.handle_callback(incoming(5), "certificate:confirm")
    assert stale_confirm.text == PAUSED_TEXT
    assert not store.aid_requests


@pytest.mark.parametrize("index,target", tuple(enumerate(("s34", "s35", "s38"))))
async def test_paused_certificate_choice_uses_existing_consultation_flow(flow, index, target):
    service, record, store = flow
    await service.scenario_flow.show(record, "s22")
    paused = await service.scenario_flow.callback(record, "aid:food_card", "draft")
    choice = paused.choices[index]
    assert choice.label == CONSULTATION_LABELS[index]

    preview = await service.handle_callback(incoming(6), choice.id)
    assert preview.audit["scenario_screen"] == target
    assert record.pending_aid_id == AIDS[target]
    assert not store.aid_requests
    assert not any(action[3].get("kind") == "consultation_requested" for action in store.actions)

    confirm = next(choice.id for choice in preview.choices if choice.id.startswith("sc:"))
    done = await service.handle_callback(incoming(7), confirm)
    assert done.audit["scenario_screen"] == SCREENS[target]["buttons"][0]["target"]
    assert sum(action[3].get("kind") == "consultation_requested" for action in store.actions) == 1
    await service.handle_callback(incoming(7), confirm)
    assert sum(action[3].get("kind") == "consultation_requested" for action in store.actions) == 1


async def test_back_from_consultation_restores_pause_and_rejects_stale_confirmation(flow):
    service, record, store = flow
    await service.handle_callback(incoming(2), "continue")
    await service.handle_callback(incoming(3), "need:food_money")
    paused = await service.handle_callback(incoming(4), "aid:food_card")
    preview = await service.handle_callback(incoming(5), paused.choices[0].id)
    confirm = next(choice.id for choice in preview.choices if choice.id.startswith("sc:"))
    back = next(choice.id for choice in preview.choices if choice.id.startswith("back:"))

    returned = await service.handle_callback(incoming(6), back)
    stale = await service.handle_callback(incoming(7), confirm)

    assert returned.text == stale.text == PAUSED_TEXT
    assert record.scenario["certificate_paused"] is True
    assert not store.aid_requests
    assert not any(action[3].get("kind") in {"consultation_requested", "certificate_waitlist"}
                   for action in store.actions)


@pytest.mark.parametrize("legacy", (False, True))
async def test_old_waitlist_prompt_cannot_submit_a_new_certificate_request(flow, legacy):
    service, record, store = flow
    await store.update(
        record, state="certificate_unavailable" if legacy else "scenario",
        scenario={} if legacy else {"screen": "i8", "variant": 0, "aid_id": "food_card"},
    )
    callback = "certificate:request" if legacy else f"sc:i8:0:{record.navigation['revision']}"

    turn = await service.handle_callback(incoming(11), callback)

    assert turn.text == PAUSED_TEXT
    assert not store.aid_requests
    assert not any(action[3].get("kind") == "certificate_waitlist" for action in store.actions)
    assert not store.escalations


@pytest.mark.parametrize("legacy", (False, True))
async def test_previously_submitted_waitlist_acknowledgement_is_unchanged(flow, legacy):
    service, record, store = flow
    await store.update(
        record, state="scenario",
        scenario={"screen": "i8", "variant": 1, "aid_id": "food_card", "submitted": True},
    )
    before = service.scenario_flow.current(record)
    callback = "certificate:request" if legacy else f"sc:i8:0:{record.navigation['revision']}"
    replay = await service.handle_callback(incoming(12), callback)

    assert replay.text == before.text
    assert replay.text != PAUSED_TEXT
    assert not any(action[3].get("kind") == "certificate_waitlist" for action in store.actions)


async def test_saved_certificate_preview_cannot_create_a_new_request(flow):
    service, record, store = flow
    store.certificates.append(StoredCertificate(
        aid_id="food_card", provider="test", nominal_rubles=300,
        activation_code="TEST-ONLY", serial_number="test-pending",
        expires_at=datetime.now(UTC) + timedelta(days=30),
    ))
    async def forbidden_claim(*args):
        raise AssertionError("new certificate claim must not run")
    store.certificate_claim = forbidden_claim
    # This is the persisted draft shape from before availability was paused.
    await store.update(
        record, state="certificate_preview", pending_aid_id="food_card",
        scenario={"screen": "s31", "variant": 0, "draft_key": "saved-draft"},
    )
    before = service.scenario_flow.current(record)
    assert any(choice.id == "certificate:confirm" for choice in before.choices)

    turn = await service.handle_callback(incoming(8), "certificate:confirm")
    assert turn.text == PAUSED_TEXT
    assert not store.aid_requests
    assert store.certificates[0].issued_at is None

    # A model-authorized completion cannot bypass the same business guard.
    await store.update(record, state="certificate_preview", pending_aid_id="food_card")
    bridge = await service._execute_resolved_turn(
        record, ResolvedTurn(text="test", effect=PolicyEffect.COMPLETE_CONTACT),
        request_key="model-completion",
    )
    assert bridge.text == PAUSED_TEXT
    assert not store.aid_requests
    assert store.certificates[0].issued_at is None


async def test_already_persisted_request_is_recovered_without_another_claim(flow):
    service, record, store = flow
    certificate = StoredCertificate(
        aid_id="food_card", provider="test", nominal_rubles=300,
        activation_code="TEST-ONLY", serial_number="test-persisted",
        expires_at=datetime.now(UTC) + timedelta(days=30),
    )
    store.certificates.append(certificate)
    request_key = service._callback_request_key(record, "certificate:confirm", 9)
    request = await store.create_aid_request(record, "food_card", None, None, request_key=request_key)
    issued_at = certificate.issued_at
    await store.update(
        record, state="certificate_preview", pending_aid_id="food_card",
        scenario={"screen": "s31", "variant": 0},
    )

    turn = await service.handle_callback(incoming(9), "certificate:confirm")

    assert turn.text != PAUSED_TEXT
    assert store.aid_requests == [request]
    assert certificate.issued_at == issued_at


async def test_reserved_certificate_resumes_by_existing_issuance_without_new_claim(flow):
    service, record, store = flow
    calls = []

    async def forbidden_claim(*args):
        raise AssertionError("new certificate claim must not run")

    async def resume(aid_id, issuance_key, recipient_id):
        calls.append((aid_id, issuance_key, recipient_id))
        return CertificateClaimResult("issued", StoredCertificate(
            aid_id=aid_id, provider="test", nominal_rubles=300,
            activation_code="TEST-ONLY", serial_number="test-reserved",
            expires_at=datetime.now(UTC) + timedelta(days=30), issuance_key=issuance_key,
        ))

    store.certificate_claim, store.certificate_resume = forbidden_claim, resume
    await store.update(
        record, state="certificate_preview", pending_aid_id="food_card",
        scenario={"screen": "s31", "variant": 0},
    )

    turn = await service.handle_callback(incoming(10), "certificate:confirm")
    replay = await service.handle_callback(incoming(10), "certificate:confirm")

    assert turn == replay
    assert turn.text != PAUSED_TEXT
    assert calls == [("food_card", service._callback_request_key(record, "certificate:confirm", 10), 7)]
    assert not store.aid_requests


@pytest.mark.parametrize("matching_key,recipient_id", ((True, 7), (False, 7), (True, 8)))
@pytest.mark.parametrize("status", ("reserved", "failed", "submitted", "delivered"))
async def test_inventory_resume_is_exact_and_select_only(matching_key, recipient_id, status):
    inventory = object.__new__(CertificateInventory)
    inventory._identity_key, inventory._account_id = b"test-only", 1
    now = datetime.now(UTC)
    row = {
        "selected_aid_id": "food_card", "provider": "test", "nominal_rubles": 300,
        "activation_code": "TEST-ONLY", "serial_number": "test-reserved",
        "valid_from": None, "expires_at": now + timedelta(days=30), "reserved_at": now,
        "issuance_key": "existing-issuance", "status": status,
        "recipient_hash": inventory._recipient_hash(7), "pdf_bucket": None,
        "pdf_object_key": None, "pdf_version_id": None, "pdf_sha256": None,
        "pdf_size": None, "pdf_filename": None, "is_test": True,
    }
    queries = []

    class Result:
        def __init__(self, value):
            self.value = value

        def mappings(self):
            return self

        def first(self):
            return self.value

    class Connection:
        async def execute(self, statement, params):
            query = str(statement)
            queries.append(query)
            assert query.lstrip().startswith("SELECT ")
            assert "c.issuance_key = :issuance_key AND c.recipient_hash = :recipient_hash" in query
            matches = all(params[key] == row[key] for key in ("issuance_key", "recipient_hash"))
            return Result(row if matches else None)

    class Engine:
        @asynccontextmanager
        async def connect(self):
            yield Connection()

    inventory._engine = Engine()
    result = await inventory.resume(
        "food_card", "existing-issuance" if matching_key else "new-issuance", recipient_id,
    )
    assert result.status == ("issued" if matching_key and recipient_id == 7 else "unavailable")
    assert (result.certificate is not None) == (matching_key and recipient_id == 7)
    assert len(queries) == 1
    assert row["status"] == status


async def test_previously_issued_certificate_screen_is_unchanged(flow):
    service, record, store = flow
    await store.update(
        record, state="scenario", scenario={"screen": "s31b", "variant": 0, "aid_id": "food_card"},
    )
    turn = await service._state_turn(record)
    assert turn == render("s31b", revision=record.navigation["revision"], context=record.scenario)
    assert turn.text != PAUSED_TEXT
    assert not store.aid_requests
