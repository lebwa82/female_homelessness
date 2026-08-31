# MVP: Telegram-агент Невидимого фонда

Русскоязычный первый разговор для женщин в ситуации бездомности. В самом Telegram-диалоге бот говорит от лица фонда; текущий контур используется только командой для проверки. Telegram — единственный подключённый канал, а `ConversationService` изолирован от aiogram: следующим адаптером может стать Chatwoot без переноса продуктовой логики.

Что реализовано:

- бережное приветствие и открытый разговор по принципам мотивационного интервью и peer support;
- любой конкретный выбор — inline-кнопка, свободный текст можно написать на каждом шаге;
- путь уровня 1: потребность → 3 подходящих варианта → один выбранный вариант → город при необходимости → способ связи → заявка;
- варианты связи: текущий Telegram, другой Telegram, телефон, email или «позже»;
- кнопка «Поговорить с живым человеком» на каждом экране; пока это сохраняет simulated-эскалацию в PostgreSQL и не прекращает разговор с ботом;
- два параллельных структурированных вызова Qwen дают только диагностические наблюдения; локальная policy владеет риском, UI, workflow и side effects;
- критический риск перекрывает обычный сценарий; для суицидального кризиса неизменно показан `8-800-2000-122`;
- один фолоуап через 7 дней и не более одного напоминания через 48 часов;
- локально ограниченный по сроку журнал в PostgreSQL 18; `/delete` удаляет связанную identity/conversation data без сохранения нового события;
- Presidio маскирует PII перед model context, Telegram-handles и typed workflow-контакты заменяются ровно на `[CONTACT]`; публичный suffix-list не обновляется во время обработки;
- курируемая база знаний пропускает в модель только approved, неистёкшие статьи с источником и датой проверки.

## Быстрый запуск

1. В `.env` добавьте `TELEGRAM_BOT_TOKEN` и `YANDEX_AI_API_KEY`. Не коммитьте `.env`.
2. Установите [just](https://github.com/casey/just) через Homebrew: `brew install just`.
   На VM должен быть заранее установлен Homebrew for Linux; не используйте устаревший
   пакет Ubuntu `apt install just`.
3. Один раз подготовьте зависимости: `just setup`.
4. Запустите Postgres и бота: `just run`.
5. Проверка: `just check`.

Для быстрой проверки бизнес-пути без Telegram и LLM: `just scenario-smoke`.

## Поведение разговора и приёмка

Свободный разговор — режим по умолчанию: просьба выслушать или выговориться
продолжает разговор с ботом и сама по себе не создаёт handoff. Кнопка «Поговорить
с живым человеком» остаётся доступна всегда, но переход к человеку создаётся
только по явному запросу или по кризисной политике. Меню появляются только в
конкретных workflows помощи, контакта и follow-up; в обычном разговоре общего
меню потребностей нет.

Психолога бот сначала предлагает в разговоре. Осторожный интерес показывает
кнопку подтверждения, а явное согласие или её выбор запускает запрос контакта;
только после контакта создаётся заявка. Историческое значение
`human_requested` может оставаться в старых строках аудита, но новый код не
создаёт его как уровень риска.

Перед деплоем staged artifact проходит `just check`, `just scenario-smoke`,
`just eval-dialogues` и `just db-assure` до activation/restart. `just eval-dialogues` воспроизводит
все versioned cases через `ConversationService` с offline diagnostics и проверяет
мутационную инвариантность policy. Команда `just eval-dialogues-live` прогоняет
тот же обезличенный набор против настроенной Qwen и расходует оплачиваемые токены
Yandex AI Studio; запускайте её сознательно после проверки ключа. Поведенческие
hard failures блокируют приёмку, а drift диагностических label виден отдельно и
не меняет deterministic UI/state/effect. Команды печатают только ID сценариев,
типы диагностик, структурированные hard projections и счётчики — не истории и
не текст ответов.

Доставка Telegram имеет семантику **bounded at-least-once**. Результат хода и
его business effects сохраняются до отправки, поэтому retry не запускает заново
диагностики и не создаёт вторую заявку или эскалацию. Но Telegram Bot API не
принимает idempotency key: в узком окне **post-send/pre-ack** успешная отправка
может быть повторена worker-ом, если подтверждение доставки не сохранилось.
Такой outcome остаётся reclaimable и получает конечную наблюдаемую категорию
`delivery_ambiguous`; обычный ACK подавляет replay, а сбой необязательного
assistant-аудита его не открывает. Это не гарантия exactly-once видимой доставки.

`just run` перед запуском завершает предыдущий локальный экземпляр этого бота,
поэтому long polling не конфликтует сам с собой. Затем бот работает в foreground;
остановить его можно `Ctrl+C`. Полный список команд — `just`. Данные Postgres
сохраняются после `just db-down`.

Логи каждого локального запуска дублируются в `.runtime/bot.log`; посмотреть их
в реальном времени можно командой `just logs`. Эта папка не коммитится.

### Прокси для Telegram Bot API

Если сервер не имеет прямого доступа к `api.telegram.org`, добавьте в `.env`
или `/etc/women-help-bot.env` одну переменную:

```dotenv
TELEGRAM_PROXY_URL=socks5://login:password@proxy.example:1080
```

Поддерживаются `http://`, `https://`, `socks4://`, `socks4a://`, `socks5://` и
ссылка Telegram-клиента вида `tg://socks?server=…&port=…&user=…&pass=…`.
Ссылка `tg://proxy?server=…&secret=…` — это MTProto, она не подходит для HTTP
Telegram Bot API.

## Постоянный запуск на VM

После первого деплоя проекта в `/opt/women-help-bot` и `uv sync --all-groups`
создайте файл `/etc/women-help-bot.env` с теми же переменными, что и в локальном
`.env`, с правами `0600`. Затем установите unit:

```bash
sudo install -m 0644 deploy/women-help-postgres.service /etc/systemd/system/women-help-postgres.service
sudo install -m 0644 deploy/women-help-bot.service /etc/systemd/system/women-help-bot.service
sudo systemctl daemon-reload
sudo systemctl enable --now women-help-postgres
sudo systemctl enable --now women-help-bot
sudo systemctl status women-help-bot
```

Логи: `sudo journalctl -u women-help-bot -f`. Отдельный unit управляет Postgres
через rootful Podman Compose, а unit бота перезапускает только Python-процесс.
На подготовленной MVP VM это выбранный режим: rootless Podman там не работает.

### Деплой

Из чистого рабочего дерева после коммита запустите:

```bash
just deploy-prod
```

Команда получает актуальный production-host через настроенный resolver и отправляет
на VM только содержимое текущего Git-коммита (без `.env` и других неотслеживаемых
файлов). На VM она распаковывает revision в staging, запускает offline checks и
PostgreSQL assurance из staged artifact через существующий root-only EnvironmentFile,
требует healthy существующий PostgreSQL container и только затем atomically activates
release. При неуспешном restart прежний release восстанавливается. Скрипт не печатает
EnvironmentFile и не запускает/пересоздаёт PostgreSQL container. Другой SSH-хост можно передать как
`just deploy-prod user@example.org`.

Проверить доступ к модели двумя реальными структурированными вызовами: `just llm-health`.

## Chatwoot: тестовый операторский контур

В этом контуре Chatwoot — единственное постоянное хранилище переписки и
назначений. Telegram после переключения станет только транспортом; Python
Agent Bot не подключается к PostgreSQL и читает историю через API Chatwoot.
Пока старый aiogram-бот продолжает принимать тестовые сообщения: запуск
Chatwoot сам по себе его не останавливает.

Контур поднимается одной командой из чистого закоммиченного дерева:

```bash
just deploy-chatwoot-test
```

На VM появляются два root-owned файла с правами `0600`:

- `/etc/women-help-chatwoot.env` — локальная PostgreSQL Chatwoot, Redis,
  публичные имена и секрет webhook;
- `/etc/women-help-agent.env` — ключ Yandex AI Studio и доступы Agent Bot.

Если у VM нет своей DNS-зоны, скрипт использует два имени `sslip.io` с её IP и
получает для них TLS через Caddy. Это допустимо только для тестового контура.
Состояние без раскрытия переменных: `just chatwoot-check`; системные логи:
`just chatwoot-logs`.

Для совместимости Agent Bot сейчас закреплён на Chatwoot `v4.12.1`: в
self-hosted выпусках 4.13+ подтверждена регрессия создания Agent Bot. Этот
релиз не передаёт HMAC-подпись для Agent Bot, поэтому webhook защищён отдельным
высокоэнтропийным секретом в URL. После перехода на исправленный Chatwoot
нужно дополнительно задать выданный им `CHATWOOT_WEBHOOK_HMAC_SECRET`: сервис
в этом режиме принимает только подписанные доставки.

Первый доступ к панели требует один раз создать администратора в Chatwoot.
На время этого действия `ENABLE_ACCOUNT_SIGNUP=true`; сразу после регистрации
измените его на `false` в `/etc/women-help-chatwoot.env` и перезапустите
`women-help-chatwoot.service`. Затем в панели создайте команду дежурных,
Telegram inbox и Agent Bot. Его outgoing URL — адрес `agent` из этого же
файла с путём `/webhooks/chatwoot/agent/<CHATWOOT_WEBHOOK_SECRET>`; секрет
вставляется из файла только локально на VM и не должен попадать в переписку
или логи.

В `/etc/women-help-agent.env` после настройки панели укажите
`CHATWOOT_BASE_URL`, `CHATWOOT_ACCOUNT_ID`, `CHATWOOT_INBOX_ID` и
`CHATWOOT_READ_TOKEN`. Затем выполните:

```bash
just chatwoot-bootstrap
```

Команда идемпотентно создаст или найдёт дежурную команду и Agent Bot, привяжет
его к inbox, запишет выданные `CHATWOOT_BOT_TOKEN` и
`CHATWOOT_DUTY_TEAM_ID` в root-only файл и запустит
`women-help-chatwoot-agent.service`. Значения токенов в терминал не выводятся.

Только после smoke-проверки «Telegram → Chatwoot timeline → один ответ Agent
Bot» можно остановить `women-help-bot` и передать его Telegram token нативному
inbox Chatwoot. При эскалации Agent Bot сначала фиксирует `reply_owner=human`,
назначает дежурную команду и открывает разговор; затем перестаёт отвечать. Для
возврата боту дежурная явно ставит `reply_owner=bot`, снимает назначение и
возвращает разговор в `pending`.

Автоматического удаления данных в этом контуре нет: срок хранения и сквозное
удаление остаются отдельным техдолгом до внешнего запуска.

## Qwen3.6 в Yandex AI Studio

В коде заданы folder ID и модель `qwen3.6-35b-a3b`; нужен только сервисный
ключ `YANDEX_AI_API_KEY`. Интеграция использует Responses API через PydanticAI
и на каждом запросе отключает серверное логирование. Модель может вести короткий
разговор в рамках встроенных навыков, но не получает права самостоятельно создать
заявку, выбрать несуществующую помощь или подавить кризисную эскалацию: это делает
только backend через `ConversationPolicy` после детерминированной локальной проверки.

## Контекст диалога

Сообщения и contact points получают configurable `MESSAGE_RETENTION_DAYS`; истёкшие
строки исключены из чтения ещё до purge, а worker повторяет purge после transient
ошибки. На обычном некризисном ходе в Yandex AI Studio идёт только redacted context
с отключённым серверным логированием. Typed workflow-контакты всегда становятся
`[CONTACT]` в current и historical model view. `/delete` в одной транзакции удаляет
identity, conversation, messages, agent/risk/action/escalation/event rows, контакты,
заявки, callbacks и follow-ups; confirmation deliberately не сохраняется, чтобы не
создать conversation заново. Provider audit сохраняет только allow-listed categories
и counts, а не provider-controlled keys или raw text.

## Перед пилотом

Замените демонстрационные статьи в `knowledge/verified_resources.json` реальными,
утверждёнными маршрутами. Для каждой нужны владелец, источник, дата проверки,
срок истечения и статус `approved`. До внешнего пилота подключите Chatwoot или
другой операторский контур, задайте расписание/ожидание дежурных, реальные
маршруты выдачи помощи и политику для несовершеннолетних.

Qwen включён для коротких эмпатичных реплик и не является единственным барьером
кризисной эскалации. Перед пилотом всё ещё нужны утверждённая модель данных,
согласия и отдельный тест кризисного классификатора на русскоязычных
обезличенных кейсах.
