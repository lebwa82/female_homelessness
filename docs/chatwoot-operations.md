# Telegram и Chatwoot: эксплуатация

Telegram подключён штатным каналом Chatwoot. Новая переписка хранится в БД
Chatwoot; Python получает подписанные события и сохраняет ответы через API.
Прежний `women-help-bot` после переключения должен быть остановлен и отключён
от автозапуска. Его БД не удаляется; старые сообщения автоматически не импортируются.

## Где искать сообщения

В кабинете откройте Conversations → All. Статус `Pending` означает, что диалог
ведёт бот; стандартный фильтр `Open` такие диалоги скрывает. Выберите все статусы
или `Pending`. Рабочий канал называется «Telegram — Невидимый фонд».

Канал «Техническая проверка интеграции» нужен только для серверных проверок.
Он использует тот же Agent Bot, но не отправляет сообщения пользователям Telegram.

## Подключение дежурного

- При нажатии кнопки живого человека бот записывает `reply_owner=human`,
  назначает команду «Дежурные», переводит разговор в `Open` и замолкает.
- Чтобы подключиться самостоятельно, откройте разговор (`Open`) и назначьте
  его себе. После этого можно отвечать в штатном поле Chatwoot.
- Чтобы вернуть разговор боту: установите поле «Кто отвечает» в `bot`,
  снимите назначение человека и команды и переведите разговор в `Pending`.
- `/clear` сбрасывает контекст бота, но сохраняет переписку. Кнопка назад
  восстанавливает предыдущее состояние; заявки не отменяются.
- `/system_info` показывает сборку и канал `Chatwoot → Telegram`.

## Сеть и запуск

Штатный HTTP-клиент Chatwoot использует внутренний HTTP CONNECT прокси,
который библиотека `pproxy` подключает к `TELEGRAM_PROXY_URL`. Порт прокси
не опубликован на VM; разрешено только назначение `api.telegram.org`.
Telegram TLS не расшифровывается. Настройка хранится в root-only agent env.

Сервисы: `women-help-chatwoot` (PostgreSQL, Redis, Rails, Sidekiq, Caddy,
прокси) и `women-help-chatwoot-agent` (Python). Все контейнеры перезапускаются.
Образ Python включает файлы навыков, модель Presidio и метаданные релиза.

## Первичное подключение на VM

После установки Chatwoot и создания первого администратора, от root:

```sh
python3 /opt/women-help-chatwoot/deploy/chatwoot/activate.py backup
python3 /opt/women-help-chatwoot/deploy/chatwoot/activate.py prepare
python3 /opt/women-help-chatwoot/deploy/chatwoot/activate.py provision
```

Сначала соберите образ и проверьте прокси/обработчик. Перед `connect`
обязательно остановите старый polling — два обработчика нельзя включать вместе.

```sh
systemctl stop women-help-bot
python3 /opt/women-help-chatwoot/deploy/chatwoot/activate.py connect
systemctl disable women-help-bot
python3 /opt/women-help-chatwoot/deploy/chatwoot/activate.py status
```

`provision` сохраняет служебные токены и ключ подписи только на VM.
`connect` устанавливает нативный Telegram webhook; повторный запуск не создаёт
дубликаты. Проверьте HTTPS health, отсутствие новых ошибок webhook, появление
входящего сообщения и единственного ответа, затем передачу дежурному.

Для отката после подключения сначала остановите Agent Bot и удалите Telegram
webhook через Bot API, сохраняя pending updates. Только затем запускайте старый
polling. Не восстанавливайте старую БД поверх новых обращений. Резервные копии
сохраняются в `/opt/women-help-backups/chatwoot-connect.*` с закрытыми правами.
