# Мониторинг Невидимого фонда

Пять проверок раз в минуту: приём Telegram, его прокси, Python-обработчик,
очереди Redis и heartbeat самого сервера. Закрытый кабинет Better Stack —
дашборд состояния и истории; публичная status page не создаётся.
Получатель email: `igor.cherentsov@gmail.com`. Дополнительный Telegram-канал
`-1004392684857` обслуживает существующий `@nevidima_admin_bot`.

## Что считается сбоем

| Проверка | Условие |
| --- | --- |
| Приём | Отсутствующая, некорректная или старше 120 секунд отметка `last_poll` |
| Прокси | Туннель неактивен или `getMe` через существующий прокси не проходит два раза подряд |
| Обработчик | Контейнер не запущен либо внутренний проверенный HTTPS `/healthz` не отвечает 200 два раза подряд |
| Очереди | Старейшая готовая, неподтверждённая или просроченная retry/scheduled задача старше 180 секунд в двух проверках подряд |
| Сервер | Better Stack не получает heartbeat в течение периода 60 + grace 60 секунд |

Ожидание будущего scheduled/retry задания нормально. Для Streams проверяются
все восемь линий и группа `agent`. Redis исполняет ограниченную read-only Lua
проекцию: наружу выходят только количества и временные отметки. Проекция
внутри Redis читает ID событий и временные поля Sidekiq JSON; содержимое
задач, диалогов и сертификатов не возвращается и не логируется. Невозможность
прочитать метаданные — ошибка проверки, а не нулевая очередь.

Пустая очередь и отсутствие сообщений от клиенток нормальны. `getMe` не
получает входящие и не отправляет ответы. Полный автоматический тест диалога
не входит в эти проверки. Успешный `/healthz` отдельно не доказывает доставку.

## Устройство

`nevidimy-monitor.timer` запускает отдельный oneshot раз в минуту, общий
срок — 45 секунд. Он не зависит через Requires от рабочих unit: падение
приложения не останавливает watchdog. Хост проверяет Docker/systemd и
отправляет свой heartbeat до внутренних проверок. Для HTTP/Redis используется
отдельный одноразовый контейнер Compose, проект `nevidimy-monitor`, 128 МиБ,
read-only filesystem, UID 65534, без capabilities и без Docker socket.

Образ берётся по неизменяемому ID текущего backend; приложение в нём не
запускается. Отдельный маленький probe получает только выбранные настройки
через stdin, использует существующую сеть и CA. Команда запуска проходит через
`scripts/chatwoot_compose.py --monitoring`; она не может активировать основной
Compose project. Основной release `/opt/nevidimy/app` не переключается.

Прямого маршрута в Telegram нет. Heartbeat Better Stack идёт отдельным HTTPS
с хоста, без Telegram-прокси. В логах нет URL heartbeat, токенов и сырых ошибок.
Доступы к основному боту и админ-боту читаются в памяти из текущих контейнеров;
копии токенов в новой конфигурации не создаются.

Состояние сохраняется атомарно в `/var/lib/nevidimy-monitor/state.json` (0600,
каталог 0700), проверяется перед использованием. Начальная неопределённость
не считается успехом. Восстановление требует двух успешных проверок подряд.
Telegram-уведомления: начало, напоминание каждые 30 минут, восстановление.
При недоступности Telegram хранится последнее уведомление по каждому
компоненту, затем отправляется сводный результат, без очереди старых повторов.
Как и обычный Telegram sendMessage, доставка не имеет exactly-once гарантии:
в окне отправки до сохранения подтверждения возможен повтор.

Email и история инцидентов обеспечиваются самим Better Stack. Период его
напоминаний нужно сверять в кабинете отдельно: локальный 30-минутный таймер
управляет только Telegram. Без внешних URL результат явно показывает
`external_configured=false`: локальная служба не заменяет внешнее оповещение
о падении всего сервера.

## Подключение

Сначала подтвердить VM `9351701`, имя, проект и актуальный IP через Timeweb API,
закреплённый host key и marker `timeweb-nevidimy-prod-app-msk-1`. Передавать
только архив Git-коммита, без `.env`, `.runtime` и локальных секретов.

Разместить отдельный release в `/opt/nevidimy/monitor/releases/<revision>`.
Включить `app/__init__.py`, `app/monitoring`, host/probe/install/compose scripts,
`deploy/timeweb/compose.monitor.yml`, watchdog unit/timer и пример env. Не
перезаписывать старые selective releases приложения, образы, токены или cursor.

Создать пять heartbeat с периодом 60 и grace 60 секунд, email включён,
звонки/SMS/push отключены. Бесплатный аккаунт направляет email всем участникам:
сверить, что единственный получатель — согласованный адрес. Внешние проверки
можно создать через кабинет или опциональный `scripts/setup_bot_monitoring.py`:
его `--plan` только читает, `--apply` требует проверенную маршрутизацию.
Этот helper не требуется для работы watchdog; API credential не хранится на VM.

Секретные URL сохранить как root-only `/etc/nevidimy-monitor.env` (0600):

```dotenv
BOT_MONITOR_HEARTBEAT_URLS_JSON={"telegram_poll":"<secret URL>","telegram_proxy":"<secret URL>","agent":"<secret URL>","queues":"<secret URL>","host":"<secret URL>"}
BOT_MONITOR_DASHBOARD_URL=https://incidents.betterstack.com/team/t629209/heartbeats
BOT_MONITOR_OWNER_EMAIL=igor.cherentsov@gmail.com
BOT_MONITOR_TELEGRAM_CHAT_ID=-1004392684857
```

JSON записывается как данные, без внешних shell-кавычек. Файл никогда не source/eval.
Installer сохраняет существующую конфигурацию и state, делает резервную копию
только файлов мониторинга; штатный запуск:

```bash
python3 scripts/install_bot_monitoring.py --revision <40hex> --plan
python3 scripts/bot_monitor.py --dry-run
python3 scripts/bot_monitor.py --test-telegram
python3 scripts/install_bot_monitoring.py --revision <40hex> --apply
```

После запуска проверить таймер, два последовательных журнала со свежими
heartbeat и кабинет Better Stack. Тест email выполнить отдельным тестовым
инцидентом, без остановки работающего бота. Пока получение не подтверждено,
не утверждать, что доставка email проверена. Существующую неисправность
показывать как сбой, без искусственного успешного heartbeat для запуска.

Откат: остановить только `nevidimy-monitor.timer`, дождаться завершения его
oneshot; вернуть предыдущую ссылку `monitor/current` и watchdog units из
`monitor/backups`, сделать daemon-reload. Данные приложения не откатывать.

## Проверки кода

`uv run pytest tests/test_bot_monitor_*.py tests/test_setup_bot_monitoring.py
tests/test_install_bot_monitoring.py tests/test_chatwoot_compose_entrypoint.py -q --tb=short`.
Дополнительные Lua-тесты используют Redis 7+ в изолированном пустом DB15:
`TEST_MONITOR_REDIS_URL=redis://127.0.0.1:16379/15`.
Не указывать для этих тестов рабочий Redis.
