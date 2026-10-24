import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from app import certificate_admin_bot as admin_bot
from app.certificate_admin import CertificateAdmin
from app.support_prompt import MAX_PROMPT_BYTES, PromptStore, PromptStoreError


def owner(user_id=10, role="owner"):
    return CertificateAdmin(user_id, "Test Owner", role, False)


def message(user_id=10, filename="instruction.txt", size=20, chat_type="private"):
    return SimpleNamespace(
        from_user=SimpleNamespace(id=user_id, full_name="Test Owner"),
        chat=SimpleNamespace(id=user_id, type=chat_type),
        document=SimpleNamespace(file_name=filename, file_size=size),
        answer=AsyncMock(),
    )


def callback(data, user_id=10, chat_type="private"):
    return SimpleNamespace(
        from_user=SimpleNamespace(id=user_id),
        message=SimpleNamespace(chat=SimpleNamespace(type=chat_type)),
        data=data,
        answer=AsyncMock(),
    )


@pytest.fixture
def ui(tmp_path):
    repository = AsyncMock()
    repository.admin.return_value = owner()
    store = PromptStore(tmp_path / "prompt")
    bot = AsyncMock()
    return admin_bot._PromptControls(repository, store), repository, store, bot


async def draft(controls, bot, text="Keep answers brief.", user_id=10):
    await controls.callback(callback("ai:upload", user_id), bot)

    async def download(document, *, destination, chunk_size):
        assert chunk_size == MAX_PROMPT_BYTES + 1
        destination.write(text.encode("utf-8"))
        return destination

    bot.download.side_effect = download
    await controls.document(message(user_id), bot)
    return controls._states[user_id]


def test_owner_menu_only():
    assert "Настройки ИИ" in str(admin_bot._main_keyboard(owner()))
    assert "Настройки ИИ" not in str(admin_bot._main_keyboard(owner(role="operator")))


@pytest.mark.parametrize("role", [None, "operator"])
async def test_non_owner_cannot_download_or_mutate(ui, role):
    controls, repository, store, bot = ui
    repository.admin.return_value = owner(role=role) if role else None
    await controls.menu(message(), bot)
    for action in ["ai:show", "ai:download", "ai:upload", "ai:rollback", "ai:apply:forged"]:
        await controls.callback(callback(action), bot)
    await controls.document(message(), bot)
    bot.download.assert_not_awaited()
    bot.send_document.assert_not_awaited()
    assert store.load().revision == "builtin"
    assert not controls._states


async def test_group_controls_are_ignored(ui):
    controls, repository, _, bot = ui
    await controls.menu(message(chat_type="group"), bot)
    await controls.callback(callback("ai:upload", chat_type="group"), bot)
    await controls.document(message(chat_type="group"), bot)
    repository.admin.assert_not_awaited()
    bot.send_message.assert_not_awaited()
    bot.download.assert_not_awaited()


async def test_missing_configuration_is_safe(ui):
    _, repository, _, bot = ui
    controls = admin_bot._PromptControls(repository, None)
    await controls.callback(callback("ai:upload"), bot)
    await controls.document(message(), bot)
    assert "не подключены" in bot.send_message.await_args.args[1]
    bot.download.assert_not_awaited()


async def test_preview_is_plain_text_and_apply_is_explicit_and_one_shot(ui, monkeypatch):
    controls, repository, store, bot = ui
    text = "Use <b>plain</b> text & concise answers."
    state = await draft(controls, bot, text)
    assert store.load().revision == "builtin"
    preview = next(call for call in bot.send_message.await_args_list if text in call.args[1])
    assert preview.kwargs["parse_mode"] is None
    assert preview.kwargs["link_preview_options"].is_disabled
    publish = Mock(wraps=store.publish)
    monkeypatch.setattr(store, "publish", publish)
    auth_count = 0
    both_committing = asyncio.Event()

    async def authorize(user_id):
        nonlocal auth_count
        auth_count += 1
        if auth_count == 4:
            both_committing.set()
        if auth_count in {2, 4}:
            await both_committing.wait()
        return owner(user_id)

    repository.admin.side_effect = authorize
    await asyncio.gather(*[
        controls.callback(callback(f"ai:apply:{state.token}"), bot) for _ in range(2)
    ])
    applied = store.load()
    assert applied.text == text
    assert applied.previous_revision == "builtin"
    publish.assert_called_once()
    assert not controls._states


async def test_revocation_at_commit_blocks_activation(ui):
    controls, repository, store, bot = ui
    state = await draft(controls, bot)
    repository.admin.side_effect = [owner(), None]
    await controls.callback(callback(f"ai:apply:{state.token}"), bot)
    assert store.load().revision == "builtin"
    assert not controls._states


async def test_expiration_during_owner_recheck_blocks_activation(ui, monkeypatch):
    controls, repository, store, bot = ui
    now = 100.0
    monkeypatch.setattr(admin_bot, "monotonic", lambda: now)
    state = await draft(controls, bot)
    auth_count = 0

    async def authorize(user_id):
        nonlocal auth_count, now
        auth_count += 1
        if auth_count == 2:
            now += admin_bot.PROMPT_DRAFT_TTL_SECONDS + 1
        return owner(user_id)

    repository.admin.side_effect = authorize
    await controls.callback(callback(f"ai:apply:{state.token}"), bot)
    assert store.load().revision == "builtin"
    assert not controls._states


async def test_other_owner_cannot_use_confirmation(ui):
    controls, repository, store, bot = ui
    state = await draft(controls, bot)
    repository.admin.side_effect = lambda user_id: owner(user_id)
    await controls.callback(callback(f"ai:apply:{state.token}", user_id=11), bot)
    assert store.load().revision == "builtin"
    assert controls._states[10] is state


@pytest.mark.parametrize("action", ["apply", "cancel"])
async def test_confirmation_requires_exact_callback_data(ui, action):
    controls, _, store, bot = ui
    state = await draft(controls, bot)
    await controls.callback(callback(f"ai:{action}:unexpected:{state.token}"), bot)
    assert store.load().revision == "builtin"
    assert controls._states[10] is state


async def test_upload_token_cannot_apply_before_preview(ui):
    controls, _, store, bot = ui
    await controls.callback(callback("ai:upload"), bot)
    state = controls._states[10]
    await controls.callback(callback(f"ai:apply:{state.token}"), bot)
    assert store.load().revision == "builtin"
    assert not controls._states
    assert "Сначала загрузите TXT" in bot.send_message.await_args.args[1]


async def test_cancel_replacement_and_expiration_invalidate_old_buttons(ui, monkeypatch):
    controls, _, store, bot = ui
    now = 100.0
    monkeypatch.setattr(admin_bot, "monotonic", lambda: now)
    old = await draft(controls, bot)
    current = await draft(controls, bot, "Use neutral wording.")
    await controls.callback(callback(f"ai:apply:{old.token}"), bot)
    assert controls._states[10] is current
    await controls.callback(callback(f"ai:cancel:{current.token}"), bot)
    await controls.callback(callback(f"ai:apply:{current.token}"), bot)
    assert store.load().revision == "builtin"
    expiring = await draft(controls, bot)
    now += admin_bot.PROMPT_DRAFT_TTL_SECONDS + 1
    await controls.callback(callback(f"ai:apply:{expiring.token}"), bot)
    assert store.load().revision == "builtin"
    assert not controls._states


async def test_concurrent_owner_change_rejects_stale_draft(ui):
    controls, _, store, bot = ui
    state = await draft(controls, bot)
    current = store.publish("New version by another owner.", actor_id=11, expected_revision="builtin")
    await controls.callback(callback(f"ai:apply:{state.token}"), bot)
    assert store.load() == current
    assert "уже изменилась" in bot.send_message.await_args.args[1]


async def test_rollback_requires_confirmation_and_is_one_shot(ui):
    controls, _, store, bot = ui
    first = store.publish("Version one.", actor_id=10, expected_revision="builtin")
    current = store.publish("Version two.", actor_id=10, expected_revision=first.revision)
    await controls.callback(callback("ai:rollback"), bot)
    state = controls._states[10]
    assert store.load() == current
    await asyncio.gather(*[
        controls.callback(callback(f"ai:apply:{state.token}"), bot) for _ in range(2)
    ])
    assert store.load().text == "Version one."


async def test_rollback_without_previous_revision_is_unavailable(ui):
    controls, _, _, bot = ui
    await controls.callback(callback("ai:rollback"), bot)
    assert not controls._states
    assert "нет" in bot.send_message.await_args.args[1]


async def test_rollback_rejects_intervening_change(ui):
    controls, _, store, bot = ui
    first = store.publish("First custom version.", actor_id=10, expected_revision="builtin")
    await controls.callback(callback("ai:rollback"), bot)
    state = controls._states[10]
    latest = store.publish("A newer version.", actor_id=11, expected_revision=first.revision)
    await controls.callback(callback(f"ai:apply:{state.token}"), bot)
    assert store.load() == latest
    assert not controls._states


async def test_first_custom_version_rolls_back_to_builtin(ui):
    controls, _, store, bot = ui
    store.publish("First custom version.", actor_id=10, expected_revision="builtin")
    await controls.callback(callback("ai:rollback"), bot)
    state = controls._states[10]
    await controls.callback(callback(f"ai:apply:{state.token}"), bot)
    assert store.load().text is None


@pytest.mark.parametrize(
    "filename,size", [("instruction.doc", 20), ("instruction.txt", 16385), (None, 20)]
)
async def test_file_metadata_validation_precedes_download(ui, filename, size):
    controls, _, store, bot = ui
    await controls.callback(callback("ai:upload"), bot)
    await controls.document(message(filename=filename, size=size), bot)
    bot.download.assert_not_awaited()
    assert store.load().revision == "builtin"


@pytest.mark.parametrize("payload", [b"", b"\xff", b"x" * 16385, b"hello\x00"])
async def test_actual_download_is_bounded_and_validated(ui, payload):
    controls, _, store, bot = ui
    await controls.callback(callback("ai:upload"), bot)

    async def download(document, *, destination, chunk_size):
        destination.write(payload)

    bot.download.side_effect = download
    await controls.document(message(size=1), bot)
    assert controls._states[10].kind == "upload"
    assert store.load().revision == "builtin"
    assert "не принят" in bot.send_message.await_args.args[1]


async def test_maximum_utf8_file_uses_telegram_sized_plain_text_chunks(ui):
    controls, _, _, bot = ui
    text = "\U0001f600" * (MAX_PROMPT_BYTES // 4)
    state = await draft(controls, bot, text)
    assert state.text == text
    previews = [call for call in bot.send_message.await_args_list if "parse_mode" in call.kwargs]
    assert len(previews) >= 3
    assert all(len(call.args[1].encode("utf-16-le")) // 2 <= 4096 for call in previews)
    assert all(call.kwargs["parse_mode"] is None for call in previews)


def test_bounded_buffer_rejects_cumulative_oversize():
    with admin_bot._PromptBuffer() as buffer:
        buffer.write(b"x" * MAX_PROMPT_BYTES)
        with pytest.raises(ValueError):
            buffer.write(b"x")
        assert len(buffer.getvalue()) == MAX_PROMPT_BYTES


async def test_download_error_does_not_echo_sensitive_details(ui, caplog):
    controls, _, _, bot = ui
    await controls.callback(callback("ai:upload"), bot)
    marker = "private-upload-marker"
    bot.download.side_effect = RuntimeError(marker)
    await controls.document(message(), bot)
    assert marker not in str(bot.send_message.await_args_list)
    assert marker not in caplog.text


@pytest.mark.parametrize("revoke", [False, True])
async def test_reset_or_revocation_during_download_discards_content(ui, revoke):
    controls, repository, store, bot = ui
    await controls.callback(callback("ai:upload"), bot)

    async def download(document, *, destination, chunk_size):
        destination.write(b"A short draft.")
        if revoke:
            repository.admin.return_value = None
        else:
            controls.cancel(10)

    bot.download.side_effect = download
    await controls.document(message(), bot)
    assert not controls._states
    assert store.load().revision == "builtin"


async def test_expiration_during_download_discards_content(ui, monkeypatch):
    controls, _, store, bot = ui
    now = 100.0
    monkeypatch.setattr(admin_bot, "monotonic", lambda: now)
    await controls.callback(callback("ai:upload"), bot)

    async def download(document, *, destination, chunk_size):
        nonlocal now
        destination.write(b"A short draft.")
        now += admin_bot.PROMPT_DRAFT_TTL_SECONDS

    bot.download.side_effect = download
    await controls.document(message(), bot)
    assert not controls._states
    assert store.load().revision == "builtin"
    assert "устарела" in bot.send_message.await_args.args[1]


async def test_cancel_during_preview_never_offers_confirmation(ui):
    controls, _, store, bot = ui

    async def send_message(user_id, text, **kwargs):
        if text.startswith("Черновик инструкции"):
            controls.cancel(user_id)

    bot.send_message.side_effect = send_message
    await controls.callback(callback("ai:upload"), bot)
    bot.download.side_effect = lambda document, *, destination, chunk_size: destination.write(b"Text.")
    await controls.document(message(), bot)
    assert not controls._states
    assert store.load().revision == "builtin"
    assert not any(
        "ai:apply:" in str(call.kwargs.get("reply_markup"))
        for call in bot.send_message.await_args_list
    )


async def test_builtin_and_custom_export_use_fixed_filename(ui, monkeypatch):
    controls, _, store, bot = ui
    from app import agents

    monkeypatch.setattr(agents, "SUPPORT_TONE", "Neutral default instruction.")
    await controls.callback(callback("ai:download"), bot)
    exported = bot.send_document.await_args.args[1]
    assert exported.data == b"Neutral default instruction."
    assert exported.filename == "ai-instruction.txt"
    store.publish("Custom instruction.", actor_id=10, expected_revision="builtin")
    await controls.callback(callback("ai:show"), bot)
    assert bot.send_message.await_args.args[1] == "Custom instruction."
    assert bot.send_message.await_args.kwargs["parse_mode"] is None
    await controls.callback(callback("ai:download"), bot)
    assert bot.send_document.await_args.args[1].data == b"Custom instruction."


async def test_pdf_routing_is_preserved_and_no_prompt_paths_use_filename(ui):
    controls, _, store, bot = ui
    await controls.callback(callback("ai:upload"), bot)
    assert controls.handles_document(10, "../../instruction.txt")
    assert controls.handles_document(10, "incorrect.doc")
    assert not controls.handles_document(10, "certificate.PDF")
    assert not controls.handles_document(11, "certificate.pdf")
    bot.download.side_effect = lambda document, *, destination, chunk_size: destination.write(b"Text.")
    await controls.document(message(filename="../../instruction.txt"), bot)
    state = controls._states[10]
    await controls.callback(callback(f"ai:apply:{state.token}"), bot)
    assert store.load().text == "Text."
    assert {path.name for path in store.directory.iterdir()} == {
        "support_prompt.json", ".support_prompt.lock"
    }


async def test_draft_count_and_lifetime_are_bounded(ui, monkeypatch):
    controls, repository, _, bot = ui
    monkeypatch.setattr(admin_bot, "MAX_PROMPT_DRAFTS", 1)
    repository.admin.side_effect = lambda user_id: owner(user_id)
    await controls.callback(callback("ai:upload", user_id=10), bot)
    await controls.callback(callback("ai:upload", user_id=11), bot)
    assert len(controls._states) == 1
    assert 10 in controls._states


async def test_store_errors_do_not_echo_details(ui, monkeypatch):
    controls, _, store, bot = ui

    def fail():
        raise PromptStoreError("private-store-marker")

    monkeypatch.setattr(store, "load", fail)
    await controls.callback(callback("ai:show"), bot)
    assert "private-store-marker" not in str(bot.send_message.await_args_list)


async def test_uncertain_store_write_is_not_reported_as_unchanged_or_retried(ui, monkeypatch):
    controls, _, store, bot = ui
    state = await draft(controls, bot)
    publish = store.publish

    def publish_then_fail(*args, **kwargs):
        publish(*args, **kwargs)
        raise PromptStoreError("private-write-marker")

    write = Mock(side_effect=publish_then_fail)
    monkeypatch.setattr(store, "publish", write)
    await controls.callback(callback(f"ai:apply:{state.token}"), bot)
    assert store.load().text == state.text
    assert "проверьте текущую версию" in bot.send_message.await_args.args[1]
    assert "private-write-marker" not in str(bot.send_message.await_args_list)
    await controls.callback(callback(f"ai:apply:{state.token}"), bot)
    write.assert_called_once()


async def test_dispatcher_routes_txt_and_pdf_and_menu_resets_only_prompt(ui):
    _, repository, store, bot = ui
    inventory, object_store = AsyncMock(), AsyncMock()
    dispatcher = admin_bot.build_dispatcher(repository, inventory, object_store, prompt_store=store)
    handlers = {handler.callback.__name__: handler.callback for handler in dispatcher.message.handlers}
    controls = handlers["menu"].__self__
    await controls.callback(callback("ai:upload"), bot)
    repository.active_batch.return_value = None
    await handlers["receive_pdf"](message(filename="sample.pdf"), bot)
    repository.active_batch.assert_awaited_once_with(10)
    assert controls._states[10].kind == "upload"
    await draft(controls, bot)
    await handlers["menu_command"](message(), bot)
    assert not controls._states
    await draft(controls, bot)
    await handlers["cancel_command"](message())
    assert not controls._states
    repository.cancel_batch.assert_not_awaited()


async def test_unnamed_prompt_upload_does_not_enter_certificate_pipeline(ui):
    _, repository, store, bot = ui
    dispatcher = admin_bot.build_dispatcher(
        repository, AsyncMock(), AsyncMock(), prompt_store=store
    )
    handlers = {handler.callback.__name__: handler.callback for handler in dispatcher.message.handlers}
    controls = handlers["menu"].__self__
    await controls.callback(callback("ai:upload"), bot)
    await handlers["receive_pdf"](message(filename=None), bot)
    repository.active_batch.assert_not_awaited()
    repository.add_rejected.assert_not_awaited()
    bot.download.assert_not_awaited()
    assert controls._states[10].kind == "upload"
    assert "Нужен TXT" in bot.send_message.await_args.args[1]
