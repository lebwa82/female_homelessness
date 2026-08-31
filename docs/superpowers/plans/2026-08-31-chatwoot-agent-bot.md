# Chatwoot Agent Bot — план реализации

> **Для выполнения:** использовать `superpowers:executing-plans` и выполнять пункты в этой ветке. Подзадачи не делегируются: правила проекта запрещают передавать чувствительный контекст другим агентам.

**Цель:** перенести test-контур Telegram-бота на Chatwoot как единственное постоянное хранилище разговоров. Python-приложение становится stateless Agent Bot: читает и изменяет только Chatwoot API, использует существующие Qwen-диагностики и не подключается к PostgreSQL напрямую.

**Архитектура:** Chatwoot v4.12.1 + Redis + PostgreSQL запускаются в одном Podman Compose-проекте. Telegram — нативный inbox Chatwoot. `app.chatwoot_agent` — `aiohttp` веб-сервис: принимает события Agent Bot, повторно читает Chatwoot, применяет policy и пишет ответ/приватные заметки/атрибуты через API. Постоянных таблиц Python-приложение не имеет. v4.12.1 выбрана осознанно: self-hosted 4.13+ имеет подтверждённую регрессию создания Agent Bot. Для v4.12.1 endpoint защищён высокоэнтропийным URL-secret; после обновления на исправленный release отдельный `CHATWOOT_WEBHOOK_HMAC_SECRET` включает обязательную HMAC-проверку.

**Технологии:** Python 3.14, aiohttp, Pydantic, OpenAI SDK для Yandex AI Studio, Chatwoot REST API, Podman Compose, Caddy, PostgreSQL 18, Redis 7.

## Общие ограничения

- Не читать и не выводить `.env` или production secret-файлы; проверять только наличие и безопасные метаданные.
- Не импортировать существующую историю из старой PostgreSQL-базы в Chatwoot.
- Не добавлять автоматическое удаление, TTL или запланированные follow-up сообщения: это отдельный техдолг.
- Не включать Chatwoot Captain.
- До переключения Telegram живой aiogram polling-сервис продолжает работать; его отключение — явный финальный шаг в test-окне.
- Любая ошибка операции передачи человеку блокирует отправку модельного ответа.

## Файловая карта

- Add: `app/chatwoot/contracts.py` — нормализованные входящие события и состояния разговора.
- Add: `app/chatwoot/client.py` — единственный HTTP-клиент Chatwoot.
- Add: `app/chatwoot/webhook.py` — проверка подписи, timestamp и routing события.
- Add: `app/chatwoot/service.py` — оркестрация policy поверх истории Chatwoot.
- Add: `app/chatwoot/app.py` — `aiohttp` HTTP-приложение Agent Bot.
- Add: `tests/test_chatwoot_*.py` — контрактные и поведенческие тесты без сети/LLM.
- Add: `deploy/chatwoot/compose.yml`, `deploy/chatwoot/Caddyfile`, `deploy/chatwoot/.env.example` — test-стек.
- Add: `deploy/chatwoot/bootstrap.py` — идемпотентный provisioning team/bot/inbox binding, без вывода секретов.
- Add: `deploy/chatwoot/women-help-chatwoot*.service` — systemd units test-стека и stateless Agent Bot.
- Modify: `pyproject.toml`, `uv.lock`, `justfile`, `README.md`, `scripts/deploy_prod.sh` (или выделенный `scripts/deploy_chatwoot_test.sh`).

## Task 1: Контракт Chatwoot и проверка webhook

**Files:** `tests/test_chatwoot_contracts.py`, `app/chatwoot/contracts.py`, `app/chatwoot/webhook.py`

1. Написать падающие тесты для `message_created` входящего сообщения, не-входящих/чужих событий, HMAC, старого timestamp, delivery ID и legacy delivery без подписи.
2. Запустить только этот тест и убедиться, что он падает именно из-за отсутствующих модулей.
3. Реализовать строгие контракты и constant-time HMAC verification по raw body. В legacy режиме URL-secret является credential, а message ID — deduplication key. Необрабатываемые события должны отвечать `204`, недостоверные — `401`.
4. Повторно запустить фокусный тест и зафиксировать зелёный результат.

## Task 2: Клиент Chatwoot без собственной БД

**Files:** `tests/test_chatwoot_client.py`, `app/chatwoot/client.py`, `app/config.py`

1. Написать падающие unit-тесты для запросов разговоров, истории, custom attributes, статуса/назначения, private notes и отправки ответа с `bot_turn_key`.
2. Запустить тесты, увидеть ожидаемый RED.
3. Реализовать маленький `aiohttp`-клиент с бот-токеном: API-base URL и токены приходят только из settings. В коде не должно быть SQL, asyncpg или Redis.
4. Сделать helpers идемпотентными: до создания visible reply искать `bot_turn_key` в истории; все HTTP-ошибки имеют безопасный тип без response body.
5. Запустить фокусный тест.

## Task 3: Stateless policy-оркестрация

**Files:** `tests/test_chatwoot_service.py`, `app/chatwoot/service.py`, `app/policy.py`, `app/agents.py`

1. Написать падающие тесты для: bot-owned разговора, human-owned разговора, прямого запроса живого человека, эскалации безопасности, `/clear`, повторной доставки и назначения сотрудницей во время LLM-вызова.
2. Запустить фокусный тест и подтвердить RED.
3. Реализовать orchestration, переиспользуя существующие безопасные диагностические и policy-компоненты без PostgreSQL store.
4. Хранить состояние сценария в conversation custom attributes; `/clear` увеличивает `context_epoch` и добавляет private note; извлечение model history начинается после последней такой заметки.
5. При human handoff записывать `reply_owner=human`, открывать разговор, назначать дежурную team и добавлять private note. Если любой из этих вызовов неуспешен — не отправлять видимый ответ.
6. Перед запуском Qwen и непосредственно перед post-message повторно читать current conversation. При `reply_owner=human` или человеческом assignment — молча завершать ход.
7. Преобразовать кнопки policy в Chatwoot `input_select`: Telegram inbox отправляет их как native inline-кнопки, а `value` возвращается следующим входящим ходом. Не подменять этим canonical state Chatwoot. Сопоставление callback id должно быть явно тестировано.
8. Запустить фокусные тесты.

## Task 4: HTTP surface Agent Bot

**Files:** `tests/test_chatwoot_app.py`, `app/chatwoot/app.py`, `app/chatwoot/__main__.py`

1. Написать падающие тесты HTTP endpoint: корректный подписанный event быстро получает `204`, policy запускается асинхронно; повтор delivery не создаёт второй side effect; bad signature получает `401`.
2. Запустить тесты и подтвердить RED.
3. Реализовать `aiohttp`-application с health endpoint и per-conversation `asyncio.Lock`. Не подтверждать принятие события до проверки HMAC (когда он передан) или защищённого URL-route и минимальной структуры.
4. Обработчик не логирует body, headers или исключения провайдера с пользовательским текстом.
5. Запустить фокусные тесты.

## Task 5: Test-инфраструктура Chatwoot

**Files:** `tests/test_chatwoot_deploy_files.py`, `deploy/chatwoot/compose.yml`, `deploy/chatwoot/Caddyfile`, `deploy/chatwoot/.env.example`, `deploy/chatwoot/women-help-agent.service`

1. Написать падающие tests/lint проверки pinned Chatwoot version, отсутствия `latest`, отсутствия Python PostgreSQL service и наличия only local Postgres/Redis volumes.
2. Запустить тесты и подтвердить RED.
3. Создать compose stack: `chatwoot-rails`, `chatwoot-sidekiq`, PostgreSQL 18, Redis 7, Caddy. Настроить text-only Active Storage, логическое ограничение upload и безопасные healthchecks.
4. Добавить systemd Agent Bot без database dependencies; секреты только в `/etc/women-help-chatwoot.env`, mode 0600.
5. Запустить tests/lint конфигурации.

## Task 6: Provisioning и команды оператора

**Files:** `tests/test_chatwoot_bootstrap.py`, `deploy/chatwoot/bootstrap.py`, `justfile`, `README.md`

1. Написать падающие unit-тесты, что bootstrap идемпотентно создаёт team/Agent Bot, подсоединяет уже созданный inbox и не печатает access token/secret.
2. Запустить RED.
3. Реализовать idempotent bootstrap с отдельными admin и Agent Bot secrets. Он не создаёт account или Telegram inbox автоматически: администратор создаёт их через UI, а bootstrap создаёт team/bot, привязывает inbox и сохраняет выданный Bot token в root-only runtime file.
4. Добавить `just chatwoot-bootstrap`, `just chatwoot-logs`, `just chatwoot-check`, `just deploy-chatwoot-test` и краткую русскую инструкцию для дежурной: где видеть диалог, как забрать в работу и как вернуть боту.
5. Запустить фокусные тесты.

## Task 7: Сквозная локальная проверка

1. `uv sync --all-groups` и узкие Chatwoot tests.
2. `just lint`, полный `pytest -q --tb=short`, без live LLM fixtures.
3. Поднять `podman compose` Chatwoot на локальном или VM test-контуре.
4. Прогнать безопасный HTTP smoke: health, URL-secret/подпись, повтор delivery, private escalation, status/assignment gate и exactly-once visible message с synthetic data.
5. Проверить, что Agent Bot не подключается к Postgres network endpoint и что разговор/история отображаются в Chatwoot UI.

## Task 8: Деплой на test VM и переключение Telegram

1. Выполнить preflight актуальной test VM `51.250.26.31`: CPU/RAM/disk, Podman, firewall, свободные 80/443 и статус legacy service. Не выводить secret-files.
2. Если публичный HTTPS hostname не найден, развернуть только stack и Agent Bot; не переключать native Telegram inbox, поскольку Telegram webhook требует доступный внешний URL. Зафиксировать точный следующий шаг, не подменяя его IP без TLS.
3. Если hostname/TLS доступен, выложить pinned commit на VM, создать root-owned `/etc/women-help-chatwoot.env` из безопасных ключей и запустить compose/systemd.
4. В Chatwoot UI создать Telegram inbox с действующим test token, добавить Agent Bot и дежурную team. Затем остановить legacy polling service, проверить test message — Chatwoot timeline, bot reply, assignment handoff и `bot_turn_key`.
5. Если live smoke не проходит, отвязать Agent Bot/Telegram inbox и восстановить legacy service; не терять данные и не выполнять destructive cleanup.
6. Зафиксировать commit, push, merge в `main` только после успешной test VM проверки, затем сообщить URL панели и результат переключения.

## План самопроверки

- Каждая функция имеет тест, который наблюдал RED до реализации.
- Нет строки подключения Python к Postgres и нет новых Python DB-таблиц.
- Ошибки эскалации fail closed.
- Таймаут/повтор webhook не создаёт второго customer-visible ответа.
- Для user-visible reply нет неуправляемого LLM button payload.
- Local test PostgreSQL не помечен как Managed и не содержит миграции из старой базы.
