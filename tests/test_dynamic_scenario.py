"""Deterministic stock and referral transitions without external delivery."""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest
from test_chatwoot_service import StubGateway, ordinary_evaluation

from app.domain import DiagnosticStatus, IncomingMessage
from app.scenario import (
    CERTIFICATE_PAUSED_TEXT,
    CERTIFICATES,
    certificate_context,
    copy_text,
    render,
)
from app.service import ConversationService
from app.store import CertificateClaimResult, InMemoryConversationStore, StoredCertificate


def incoming(message_id):
    return IncomingMessage(channel="chatwoot", platform_user_id=7, chat_id=23,
                           text="Synthetic input", message_id=message_id)


def certificate(aid_id="food_card", *, nominal=2000):
    return StoredCertificate(
        aid_id=aid_id, provider="Test provider", nominal_rubles=nominal,
        activation_code="SYNTHETIC-CODE", serial_number="SYNTHETIC-SERIAL",
        expires_at=datetime.now(UTC) + timedelta(days=90), is_test=True,
    )


def aid_choices(turn):
    return {choice.id.removeprefix("aid:") for choice in turn.choices
            if choice.id.startswith("aid:")}


def button(turn, label):
    return next(choice.id for choice in turn.choices if choice.label == label)


def events(store):
    return [audit for _, kind, _, audit in store.actions if kind == "scenario_event"]


@pytest.fixture
async def dynamic_flow():
    available = AsyncMock(return_value=frozenset())
    claim = AsyncMock(return_value=CertificateClaimResult("unavailable"))
    store = InMemoryConversationStore(certificate_claim=claim)
    service = ConversationService(store=store, gateway=StubGateway(ordinary_evaluation()),
                                  html_scenario=True, certificate_available=available)
    await service.start(incoming(1))
    record = await store.get(incoming(1))
    store.actions.clear()
    return service, record, store, available, claim


@pytest.mark.parametrize("need,screen,stock,normal,empty", (
    ("housing", "s21", {"hostel_3_nights"},
     {"hostel_3_nights", "legal_consultation"}, {"legal_consultation"}),
    ("food_money", "s22", {"food_card", "medicine_card"},
     {"food_card", "medicine_card"},
     {"psychologist_3_sessions", "legal_consultation", "peer_consultation"}),
    ("children", "s25", {"children_card"},
     {"children_card", "legal_consultation", "psychologist_3_sessions"},
     {"legal_consultation", "psychologist_3_sessions"}),
))
async def test_stock_changes_refresh_each_category_without_reserving(
    dynamic_flow, need, screen, stock, normal, empty,
):
    service, record, store, available, claim = dynamic_flow
    for inventory, expected, variant in (
        (frozenset(CERTIFICATES) - stock, empty, 1),
        (frozenset(stock), normal, 0),
        (frozenset(), empty, 1),
    ):
        available.return_value = inventory
        await service.scenario_flow.show(record, "s2")
        turn = await service.scenario_flow.callback(record, f"need:{need}", "menu")
        assert turn.audit["scenario_screen"] == screen
        assert record.scenario["variant"] == variant
        assert aid_choices(turn) == expected
        assert sum(choice.id == "human" for choice in turn.choices) == 1
    assert available.await_count == 3
    claim.assert_not_awaited()
    assert not store.aid_requests
    assert not store.actions


@pytest.mark.parametrize("aid_id,screen", (("food_card", "s31"), ("medicine_card", "s32")))
async def test_partial_food_stock_only_offers_available_item_and_preview_does_not_claim(
    dynamic_flow, aid_id, screen,
):
    service, record, store, available, claim = dynamic_flow
    available.return_value = frozenset({aid_id})
    menu = await service.scenario_flow.show(record, "s22")
    assert aid_choices(menu) == {aid_id}
    assert record.scenario["variant"] == 0
    preview = await service.scenario_flow.callback(record, f"aid:{aid_id}", "preview")
    assert preview.audit["scenario_screen"] == screen
    assert record.pending_aid_id == aid_id
    assert "certificate:confirm" in {choice.id for choice in preview.choices}
    assert available.await_count == 2
    claim.assert_not_awaited()
    assert not store.aid_requests
    assert not store.actions


async def test_stock_drained_after_menu_is_rechecked_before_preview(dynamic_flow):
    service, record, store, available, claim = dynamic_flow
    available.return_value = frozenset({"food_card"})
    menu = await service.scenario_flow.show(record, "s22")
    available.return_value = frozenset()
    turn = await service.scenario_flow.callback(record, button(menu, "Сертификат на продукты"),
                                                "stale-menu")
    assert turn.audit["scenario_screen"] == "i8"
    assert "certificate:confirm" not in {choice.id for choice in turn.choices}
    assert record.scenario["aid_id"] == "food_card"
    assert record.pending_aid_id is None
    assert available.await_count == 2
    assert [(event["kind"], event["reason"]) for event in events(store)] == [
        ("certificate_requested", "out_of_stock"),
    ]
    claim.assert_not_awaited()
    assert not store.aid_requests


@pytest.mark.parametrize("aid_id,preview_screen,result_screen", (
    ("food_card", "s31", "s31b"), ("medicine_card", "s32", "s32b"),
    ("hostel_3_nights", "s33", "s33c"), ("children_card", "s37", "s37b"),
))
async def test_confirmation_claims_once_preserves_expiry_and_home_returns_to_categories(
    dynamic_flow, aid_id, preview_screen, result_screen,
):
    service, record, store, available, claim = dynamic_flow
    available.return_value = frozenset({aid_id})
    issued = certificate(aid_id)
    expires = issued.expires_at
    claim.return_value = CertificateClaimResult("issued", issued)
    await service.scenario_flow.show(record, preview_screen)
    claim.assert_not_awaited()

    delivered = await service.handle_callback(incoming(2), "certificate:confirm")

    claim.assert_awaited_once()
    assert claim.await_args.args[0] == aid_id
    assert claim.await_args.args[2] == record.platform_user_id
    assert len(store.aid_requests) == 1
    assert issued.expires_at == expires
    assert record.scenario["expires_at"] == expires.isoformat()
    assert record.scenario["screen"] == result_screen
    assert issued.activation_code in delivered.text
    if aid_id != "hostel_3_nights":
        assert "2000" in "".join(delivered.text.split())
    assert issued.activation_code not in repr(record.scenario)
    await service.handle_callback(incoming(2), "certificate:confirm")
    await service.handle_callback(incoming(3), "certificate:confirm")
    claim.assert_awaited_once()
    assert len(store.aid_requests) == 1
    home = next(choice.id for choice in delivered.choices if choice.id.startswith("home:"))
    menu = await service.handle_callback(incoming(4), home)
    assert menu.audit["scenario_screen"] == "s2"
    assert record.pending_aid_id is None
    assert issued.activation_code not in menu.text
    assert len(store.aid_requests) == 1
    assert issued.expires_at == expires


async def test_atomic_claim_handles_stock_race_after_successful_preview(dynamic_flow):
    service, record, store, available, claim = dynamic_flow
    available.return_value = frozenset({"food_card"})
    await service.scenario_flow.show(record, "s31")
    claim.return_value = CertificateClaimResult("unavailable")
    result = await service.handle_callback(incoming(2), "certificate:confirm")
    await service.handle_callback(incoming(2), "certificate:confirm")
    claim.assert_awaited_once()
    assert result.audit["scenario_screen"] == "i8"
    assert record.scenario["aid_id"] == "food_card"
    assert not store.aid_requests
    assert [(event["kind"], event["reason"]) for event in events(store)] == [
        ("certificate_requested", "out_of_stock"),
    ]


async def test_already_issued_does_not_create_second_request(dynamic_flow):
    service, record, store, available, claim = dynamic_flow
    available.return_value = frozenset({"food_card"})
    claim.return_value = CertificateClaimResult("already_issued")
    await service.scenario_flow.show(record, "s31")
    result = await service.handle_callback(incoming(2), "certificate:confirm")
    await service.handle_callback(incoming(3), "certificate:confirm")
    claim.assert_awaited_once()
    assert result.audit["scenario_screen"] == "i9"
    assert record.scenario["variant"] == 1
    assert not store.aid_requests
    assert "certificate:confirm" not in {choice.id for choice in result.choices}


async def test_missing_availability_provider_keeps_legacy_preview_paused(dynamic_flow):
    service, record, store, available, claim = dynamic_flow
    service.certificate_available = None
    store.certificates.append(certificate())
    result = await service.scenario_flow.show(record, "s31")
    await service.handle_callback(incoming(2), "certificate:confirm")
    assert result.text == CERTIFICATE_PAUSED_TEXT
    assert record.scenario["screen"] == "i8"
    available.assert_not_awaited()
    claim.assert_not_awaited()
    assert not store.aid_requests


@pytest.mark.parametrize("inventory", (frozenset({"food_card"}), frozenset()))
async def test_model_catalog_only_contains_currently_available_certificates(
    dynamic_flow, monkeypatch, inventory,
):
    service, record, _, available, claim = dynamic_flow
    available.return_value = inventory
    evaluate = AsyncMock(wraps=service.gateway.evaluate)
    monkeypatch.setattr(service.gateway, "evaluate", evaluate)
    monkeypatch.setattr("app.service.find_verified_articles", lambda _: ())

    result = await service._evaluate_diagnostics(record)

    available.assert_awaited_once()
    evaluate.assert_awaited_once()
    context = evaluate.await_args.args[0]
    assert {item["id"] for item in context.catalog if item["fulfillment"] == "certificate"} == inventory
    assert "legal_consultation" in {item["id"] for item in context.catalog}
    assert context.new_certificates_unavailable is (not inventory)
    assert result is service.gateway.result
    claim.assert_not_awaited()


async def test_stock_provider_failure_skips_model_and_returns_unavailable_diagnostics(
    dynamic_flow, monkeypatch,
):
    service, record, _, available, claim = dynamic_flow
    available.side_effect = RuntimeError("synthetic-stock-failure")
    evaluate = AsyncMock(wraps=service.gateway.evaluate)
    monkeypatch.setattr(service.gateway, "evaluate", evaluate)
    monkeypatch.setattr("app.service.find_verified_articles", lambda _: ())

    result = await service._evaluate_diagnostics(record)

    available.assert_awaited_once()
    evaluate.assert_not_awaited()
    claim.assert_not_awaited()
    assert result.safety_status == DiagnosticStatus.UNAVAILABLE
    assert result.support_status == DiagnosticStatus.UNAVAILABLE
    assert result.safety is None
    assert result.support is None
    assert result.safety_audit == {"status": "unavailable", "error_type": "RuntimeError"}
    assert result.support_audit == result.safety_audit


async def test_resumed_certificate_preserves_original_nominal_and_expiry(dynamic_flow):
    service, record, store, _, claim = dynamic_flow
    service.certificate_available = None
    old = certificate(nominal=3000)
    old.issued_at = datetime.now(UTC) - timedelta(days=10)
    issued_at, expires = old.issued_at, old.expires_at
    store.certificate_resume = AsyncMock(return_value=CertificateClaimResult("issued", old))
    await store.update(record, state="certificate_preview", pending_aid_id="food_card",
                       scenario={"screen": "s31", "variant": 0})
    result = await service.handle_callback(incoming(2), "certificate:confirm")
    store.certificate_resume.assert_awaited_once()
    claim.assert_not_awaited()
    assert (old.issued_at, old.expires_at) == (issued_at, expires)
    assert record.scenario["expires_at"] == expires.isoformat()
    assert "3000" in "".join(result.text.split())
    assert "2000" not in "".join(result.text.split())
    assert not store.aid_requests


@pytest.mark.parametrize("actual_aid,delivery_screen,next_screen,instruction", (
    ("medicine_card", "s32b", "s32b", "лекарства"),
    ("hostel_3_nights", "s33b", "s33c", "хостел"),
))
async def test_food_confirmation_recovers_original_certificate_kind_and_document(
    dynamic_flow, actual_aid, delivery_screen, next_screen, instruction,
):
    service, record, store, available, claim = dynamic_flow
    available.return_value = frozenset({"food_card"})
    old = certificate(actual_aid, nominal=3000)
    old.issuance_key = "original-issuance"
    old.pdf_bucket = "synthetic-bucket"
    old.pdf_object_key = f"reserved/{actual_aid}.pdf"
    old.pdf_version_id = "original-version"
    old.pdf_sha256 = "1" * 64
    old.pdf_size = 123
    old.pdf_filename = "original.pdf"
    context = certificate_context(old)
    followup = {key: value for key, value in context.items() if key != "code"}
    claim.return_value = CertificateClaimResult("issued", old)
    await service.scenario_flow.show(record, "s31")
    assert record.pending_aid_id == "food_card"

    result = await service.handle_callback(incoming(2), "certificate:confirm")
    replay = await service.handle_callback(incoming(2), "certificate:confirm")

    claim.assert_awaited_once()
    assert claim.await_args.args[0] == "food_card"
    assert len(store.aid_requests) == 1
    assert store.aid_requests[0].aid_id == actual_aid
    assert store.aid_requests[0].certificate is old
    assert record.scenario["screen"] == next_screen
    assert record.scenario["aid_id"] == actual_aid
    assert record.scenario["expires_at"] == old.expires_at.isoformat()
    assert record.scenario["nominal_rubles"] == 3000
    assert result.audit["certificate_followup"] == followup
    assert result.audit["after_certificate_text"] == (
        copy_text("s33c") if actual_aid == "hostel_3_nights" else "Что можно сделать дальше?"
    )
    assert result.text == render(delivery_screen, context=context).text
    assert instruction in result.text.lower()
    assert context["expiry_date"] in result.text
    assert "3000" in "".join(result.text.split())
    assert "2000" not in "".join(result.text.split())
    assert result.attachment is not None
    assert result.attachment.model_dump() == {
        "bucket": old.pdf_bucket, "key": old.pdf_object_key,
        "version_id": old.pdf_version_id, "sha256": old.pdf_sha256,
        "size": old.pdf_size, "filename": old.pdf_filename,
        "issuance_key": old.issuance_key, "content_type": "application/pdf",
    }
    assert replay.attachment == result.attachment
    assert not store.certificates


@pytest.mark.parametrize("index,source", tuple(enumerate((
    "website", "friend", "organization", "leaflet",
))))
async def test_referral_choice_records_one_stable_event(dynamic_flow, index, source):
    service, record, store, _, _ = dynamic_flow
    turn = await service.scenario_flow.show(record, "s39", job_key="referral:source")
    callback = next(choice.id for choice in turn.choices if choice.id.startswith(f"sc:s39:{index}:"))
    result = await service.scenario_flow.callback(record, callback, "first-answer")
    assert result.audit["scenario_screen"] == "s39c"
    assert len(events(store)) == 1
    event = events(store)[0].copy()
    assert (event["kind"], event["source"], event["job_key"]) == (
        "referral_source", source, "referral:source",
    )
    turn = await service.scenario_flow.show(record, "s39", job_key="referral:source")
    callback = next(choice.id for choice in turn.choices if choice.id.startswith("sc:s39:0:"))
    await service.scenario_flow.callback(record, callback, "different-answer")
    assert events(store) == [event]


async def test_referral_other_waits_for_text_before_recording(dynamic_flow):
    service, record, store, _, _ = dynamic_flow
    turn = await service.scenario_flow.show(record, "s39", job_key="referral:source")
    other = await service.scenario_flow.callback(record, button(turn, "Другое"), "other")
    assert other.audit["scenario_screen"] == "s39b"
    waiting = await service.scenario_flow.callback(record, button(other, "Написать"), "write")
    assert waiting.audit["scenario_screen"] == "s39b"
    assert record.scenario["awaiting_text"]
    assert not store.actions
    result = await service.scenario_flow.text(record, "Synthetic referral", "answer")
    assert result.audit["scenario_screen"] == "s39c"
    assert len(events(store)) == 1
    assert events(store)[0]["kind"] == "referral_source"
    assert events(store)[0]["source"] == "other"
    assert events(store)[0]["text"] == "Synthetic referral"


async def test_referral_other_can_be_skipped_without_empty_text(dynamic_flow):
    service, record, store, _, _ = dynamic_flow
    turn = await service.scenario_flow.show(record, "s39b", job_key="referral:source")
    result = await service.scenario_flow.callback(record, button(turn, "Пропустить"), "skip")
    assert result.audit["scenario_screen"] == "s39c"
    assert len(events(store)) == 1
    assert events(store)[0]["kind"] == "referral_source"
    assert events(store)[0]["source"] == "skipped"
    assert "text" not in events(store)[0]


async def test_copy_referral_link_outputs_link_instead_of_repeating_thanks(dynamic_flow):
    service, record, store, _, _ = dynamic_flow
    thanks = await service.scenario_flow.show(record, "s39c", job_key="referral:source")
    copied = await service.scenario_flow.callback(record, button(thanks, "Скопировать ссылку"), "copy")
    assert copied.text == "t.me/nevidima_bot"
    assert copied.text != thanks.text
    assert record.scenario["screen"] == "s39c"
    assert record.scenario["variant"] == 1
    assert not store.actions
