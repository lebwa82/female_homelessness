# Почасовая внутренняя аналитика

Отдельный таймер Timeweb запускает один контейнер в начале каждого часа UTC.
После простоя systemd выполняет один пропущенный запуск. Контейнер не меняет
Chatwoot или Telegram, не обновляет образ и не публикует данные на сайт.
Excel содержит переписку выбранного аккаунта Chatwoot, HTML — только агрегаты
проверенного Telegram inbox. Оба файла доступны только в выделенном закрытом S3-бакете.
Значения, превышающие лимит ячейки Excel, хранятся без обрезки на листе
`__long_cells`: фильтруй по таблице, `row_id` и столбцу, соединяй `text` по `part`.

## Подготовка

1. Проверь VM по API Timeweb, закреплённому SSH-ключу и маркеру назначения,
   как описано в [процедуре для Timeweb](deploy-timeweb-from-mac.md#2-заново-проверить-api-и-ssh).
   Docker и сеть `nevidimy-timeweb_default` должны уже работать.
2. Доставь только код выгрузки (`analytics/*.py`, `analytics/internal_metrics.sql`)
   под `/opt/nevidimy/analytics/current/analytics/`. В соседнем каталоге
   `/opt/nevidimy/analytics/xlsxwriter/` размести проверенную, закреплённую в
   `uv.lock` версию XlsxWriter. Эти каталоги и существующий CA managed PostgreSQL
   `/opt/nevidimy/certs/postgresql-ca.crt` монтируются read-only. Файлы должен
   читать UID 65534; изменять код и зависимости может только оператор.
3. Подготовь `/etc/nevidimy-analytics.env` с владельцем `root:root` и режимом
   `0600` по утверждённому каналу, без вывода значений в журнал и без копирования
   в Git. Укажи `ANALYTICS_IMAGE` — точный локальный `sha256:…` ID проверенного
   образа backend с Python, asyncpg и boto3. Задай `ANALYTICS_ACCOUNT_ID`,
   `ANALYTICS_INBOX_ID`, `ANALYTICS_BOT_NAME` и
   `ANALYTICS_EXCLUDED_DISPLAY_IDS` (известные тестовые display ID через пробел).
   Для БД нужны `ANALYTICS_PG_HOST` (DNS-имя для TLS), `ANALYTICS_PG_PORT`
   (по умолчанию 5432), `ANALYTICS_PG_DATABASE`, `ANALYTICS_PG_USER` и
   `ANALYTICS_PG_PASSWORD` роли для чтения. Для хранилища — выделенные
   `ANALYTICS_S3_BUCKET`, `ANALYTICS_S3_ACCESS_KEY`, `ANALYTICS_S3_SECRET_KEY`.
   Не подключай env действующего приложения или ключ доступа к другим бакетам.

## Запуск и проверка

Установи оба файла из `deploy/timeweb/` в `/etc/systemd/system/` с режимом
`0644`. На целевой VM:

```sh
systemd-analyze verify /etc/systemd/system/nevidimy-analytics.service \
  /etc/systemd/system/nevidimy-analytics.timer
systemctl daemon-reload
systemctl start nevidimy-analytics.service
systemctl show nevidimy-analytics.service -p Result -p ExecMainStatus
journalctl -u nevidimy-analytics.service -n 15 --no-pager
```

Убедись, что без авторизации чтение запрещено. Проверь одинаковую метку
`snapshot-utc` в метаданных
`latest.xlsx` и `latest.html`. Два объекта заменяются последовательно, поэтому
несовпадение меток означает, что публикация пары не завершена. Не печатай
содержимое Excel, ключи, полный `docker inspect` или подписанные ссылки.
Только после успешной ручной проверки включи расписание:

```sh
systemctl enable --now nevidimy-analytics.timer
systemctl list-timers nevidimy-analytics.timer
```

Чтобы отключить новые запуски, выполни
`systemctl disable --now nevidimy-analytics.timer`. Текущий запуск можно
дождаться или остановить отдельно через `systemctl stop nevidimy-analytics.service`;
это не останавливает Chatwoot, backend или Telegram. Уже записанные файлы
остаются в закрытом бакете. При обновлении дождись завершения текущего запуска
перед переключением каталога кода или image ID.

## Получение файлов

Используй личный профиль S3 с правом чтения только бакета аналитики. Укажи
полное имя бакета из панели Timeweb и не добавляй ключи в аргументы команд:

```sh
aws --profile nevidimy-analytics --endpoint-url https://s3.twcstorage.ru \
  s3 cp s3://<имя-бакета>/latest.html ./latest.html
aws --profile nevidimy-analytics --endpoint-url https://s3.twcstorage.ru \
  s3 cp s3://<имя-бакета>/latest.xlsx ./latest.xlsx
```

HTML откроется локально в браузере. XLSX содержит чувствительную переписку:
не пересылай файл и не используй общий ключ команды.
