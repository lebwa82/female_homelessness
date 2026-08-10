# MVP: безопасный Telegram-помощник

Прототип первого контакта для женщин в ситуации бездомности. Он **не заменяет кризисную службу** и до готовности команды не должен рекламироваться как круглосуточная помощь.

Что есть:

- бережный вход вместо анкеты;
- базовый путь: нужда → самовывоз/сертификат → сигнал специалистке;
- моментальная rule-based детекция острых фраз и переход к человеку;
- ответы о праве и организациях только из `knowledge/verified_resources.json`;
- PostgreSQL для состояния, операционных событий и полного текста переписки;
- Telegram-адаптер, отделённый от доменной логики — так можно добавить WhatsApp/Max/VK без переписывания сценариев.

## Быстрый запуск

1. В `.env` добавьте `TELEGRAM_BOT_TOKEN` и `YANDEX_AI_API_KEY`. Не коммитьте `.env`.
2. Установите [just](https://github.com/casey/just) через Homebrew: `brew install just`.
   На VM должен быть заранее установлен Homebrew for Linux; не используйте устаревший
   пакет Ubuntu `apt install just`.
3. Один раз подготовьте зависимости: `just setup`.
4. Запустите Postgres и бота: `just run`.
5. Проверка: `just check`.

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

Проверить доступ к модели коротким обезличенным запросом: `uv run python -m scripts.llm_health_check`.

`STAFF_TELEGRAM_CHAT_ID` пока оставьте пустым. Позже это будет ID закрытой группы
дежурных специалисток: туда бот отправляет только сигнал о необходимости ручной
помощи, без текста переписки.

## Qwen3.6 в Yandex AI Studio

В коде уже заданы ваш folder ID и модель `qwen3.6-35b-a3b`; нужен только
сервисный API-ключ в `YANDEX_AI_API_KEY`. Интеграция использует `Responses API`
и на каждом запросе отключает серверное логирование. Модель формулирует только
короткую поддерживающую реплику; маршрутизацию помощи и кризисную эскалацию она
не принимает.

## Контекст диалога

Бот сохраняет полный текст входящих и исходящих сообщений в локальный Postgres и
на каждом обычном ходе передаёт весь transcript в Yandex AI Studio с отключённым
серверным логированием. Перед отправкой весь transcript локально проходит через
Presidio и русскую spaCy NER-модель: исходный текст остаётся в Postgres, а модель
видит типизированные замены персональных данных.

## Перед пилотом

Заполните `knowledge/verified_resources.json` реальными, утверждёнными маршрутами. Для каждой записи нужны владелец, дата проверки и срок следующей проверки. Настройте дежурство специалисток и SLA: бот не обещает «сейчас подключится», если нет людей на линии.

Qwen включён для коротких эмпатичных реплик и не является единственным барьером
кризисной эскалации. Перед пилотом всё ещё нужны утверждённая модель данных,
согласия и отдельный тест кризисного классификатора на русскоязычных
обезличенных кейсах.
