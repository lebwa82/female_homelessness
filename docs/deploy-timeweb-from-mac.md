# Деплой PII и внутреннего TLS с Mac на Timeweb

Инструкция для локального агента в `/Users/lebwa82/female_homelessness`.
Обновляется существующая VM `9351701` / `nevidimy-prod-app`, проект `3044129`,
регион `msk-1`; БД — Managed PostgreSQL `4221087` / `nevidimy-prod-pg`.
Cloud secret bindings и перенос приватных SSH-ключей в OpenAI не нужны.

Ветка изменений: `codex/pii-internal-tls`. Изменения добавляют распознавание
российских идентификаторов и секретов, исправляют перекрытия масок, а также
подготавливают проверенный TLS для внутренних HTTP и Redis. Managed PostgreSQL
уже использует `verify-full`; его CA и firewall сохраняются. Новое шифрование
в production ещё не включено. Исходный `architecture-1.png` сохранён; новый
`architecture-2.png` пока показывает записанную конфигурацию до активации TLS.

## 1. Получить код отдельно от текущих локальных изменений

На Mac:

```bash
cd /Users/lebwa82/female_homelessness
git fetch origin codex/pii-internal-tls
git worktree add ../female_homelessness-tls-deploy origin/codex/pii-internal-tls
cd ../female_homelessness-tls-deploy
nevidimy_revision=$(git rev-parse HEAD)
uv sync --frozen --all-groups
uv run ruff check .
uv run pytest -q --tb=short
```

Не сбрасывать dirty checkout на Mac. Если отдельный worktree уже существует,
сначала проверь его состояние; не удаляй его и не делай reset автоматически.
Python приложения — 3.14+. Локальные TLS/Compose-тесты требуют Docker Compose;
при отсутствии Docker часть тестов будет пропущена. Сам деплой Docker всё равно
потребует. Состояние последней облачной проверки: 873 passed, 28 skipped,
ruff clean; пропуски не являются живой проверкой production.

## 2. Заново проверить API и SSH

Используй существующий настроенный API-клиент Mac и инфраструктурный токен
Timeweb. Подтверди ID, имя, project, region и свежий IP VM, а также ID/project
managed-БД. Не печатай токен, полные API-ответы или `root_password`.
`just prod-host`, `just deploy-prod` и Yandex/Podman-скрипты здесь не применять.

Последний записанный адрес — `5.42.116.10`, login `root`, port `22`, hostname
`9351701-af950022.twc1.net`. Адрес из API имеет приоритет над этой записью.
Ранее закреплённый ED25519 host fingerprint:

```text
SHA256:7jsgHdG9OL1sw4jepEe8gBC+vyYhvdjaUYBaBIUw6B8
```

Для всех следующих SSH-команд используй **существующий рабочий ключ Mac**,
строгий проверенный `known_hosts`, `BatchMode=yes`, `StrictHostKeyChecking=yes`,
без agent forwarding. Не используй временный приватный ключ другого runtime.
Ниже `nevidimy_ssh` — массив этих настроек, а `nevidimy_remote` — проверенный
`root@<свежий IP>`. Значения путей определяет локальный агент из своей текущей
настройки, приватные файлы не копируются в проект:

```bash
nevidimy_remote="root@$nevidimy_host"
nevidimy_ssh=(ssh -p 22 -i "$nevidimy_identity" -o IdentitiesOnly=yes \
  -o BatchMode=yes -o StrictHostKeyChecking=yes \
  -o UserKnownHostsFile="$nevidimy_known_hosts" -o ForwardAgent=no)
"${nevidimy_ssh[@]}" "$nevidimy_remote" \
  'test "$(cat /etc/nevidimy-deployment-target)" = timeweb-nevidimy-prod-app-msk-1 && test "$(hostname -f)" = 9351701-af950022.twc1.net && id -un'
```

Остановись при несовпадении API/host key/marker. До изменений проверь фактические
units, drop-ins, Compose project `nevidimy-timeweb` и все четыре сервиса:
`nevidimy`, `nevidimy-agent`, `nevidimy-telegram`, `nevidimy-proxy-tunnel`.
Сохрани текущий закреплённый Chatwoot image/digest. Не обновляй его до `latest`.
Для прежнего локального режима используется drop-in
`/etc/systemd/system/nevidimy.service.d/http-local.conf`: UI остаётся доступен
только через SSH на `127.0.0.1:18080`. На проверке 20.10.2026 активен более поздний
`public-domain-https.conf`: общий HTTPS `chatwoot.admin.nevidima.ru`. Сохраняй этот
режим: обнови существующий public-domain drop-in из `domain-https.conf` и используй
общий Compose entrypoint; он добавляет `compose.tls-domain-https.yml` после TLS.
Не заменяй публичный сертификат внутренним и не публикуй внутренние порты.

## 3. Подготовить релиз и Python-образ до остановки сервисов

Передавай только архив Git-коммита. `.env`, `.runtime`, личные ключи и локальные
отчёты в архив не входят. На Mac из deployment worktree:

```bash
nevidimy_release="/opt/nevidimy/releases/$nevidimy_revision"
git archive --format=tar "$nevidimy_revision" |
  "${nevidimy_ssh[@]}" "$nevidimy_remote" \
  "install -d -m 0755 '$nevidimy_release' && tar -xf - -C '$nevidimy_release'"
```

На Mac с Docker Desktop собери именно `linux/amd64`. Этот путь не требует
доступа VM к Docker Hub, который ранее был недоступен:

```bash
docker build --platform linux/amd64 \
  --build-arg "RELEASE_REVISION=$nevidimy_revision" \
  --build-arg "RELEASED_AT_UTC=$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
  -t "localhost/women-help-chatwoot-agent:$nevidimy_revision" \
  -f deploy/chatwoot/Containerfile .
docker image inspect "localhost/women-help-chatwoot-agent:$nevidimy_revision" \
  --format '{{.Architecture}} {{.Id}}'
docker save "localhost/women-help-chatwoot-agent:$nevidimy_revision" | gzip -1 |
  "${nevidimy_ssh[@]}" "$nevidimy_remote" 'gzip -d | docker load'
```

Сверь `amd64` и image ID на обеих сторонах. Если сборка на Mac невозможна,
сборка на VM допустима только с её обычными разрешёнными registry/CA/cache;
не обходи сетевые ограничения и не отключай TLS-проверку. На VM далее
используются `--no-build --pull never`: все образы должны быть подготовлены.

## 4. Резервная копия на VM

Выполни через проверенный SSH, без вывода значений секретов:

1. Создай root-only каталог `/opt/nevidimy/backups/before-internal-tls.<UTC>`
   с правами `0700`; сохрани путь для отката.
2. Скопируй существующие `/etc/women-help-{chatwoot,agent,telegram,telegram-proxy}.env`,
   units `nevidimy*.service`, drop-ins и сведения о том, какие сервисы активны.
   Сохрани прежнее содержимое/ссылку `/opt/nevidimy/app` и текущие image IDs.
3. Создай проверенный custom-format `pg_dump` **managed-БД** в этот каталог,
   затем проверь `pg_restore --list` и SHA256. Подставляй реквизиты из root-only
   env внутри процесса: `PGPASSWORD` не помещать в аргументы команды или stdout.
   Обязательны `PGSSLMODE=verify-full` и
   `PGSSLROOTCERT=/opt/nevidimy/certs/postgresql-ca.crt`.
4. Сохрани Redis AOF/volume и состояние очереди штатным способом. Не выполняй
   `down -v`, очистку Redis, удаление volumes или Docker prune.
5. Сохрани прежний Python image отдельным rollback-тегом до замены `:current`.

Если резервная копия не прошла проверку, не начинай переключение. Дампы и env
остаются закрытыми файлами на VM, не передаются в Git или текстовый отчёт.

## 5. Сертификаты и перевод подписанного webhook на HTTPS

Из каталога нового релиза на VM выпусти сертификаты:

```bash
python3 -m scripts.prepare_internal_tls \
  --output-dir /opt/nevidimy/certs/internal-tls
```

Если сертификаты уже существуют, сначала проверь их состояние: генератор
намеренно не перезаписывает их без `--renew`. CA провайдера PostgreSQL остаётся
прежним. `compose.tls-postgres.yml` на Timeweb не используется.

Останови `nevidimy-telegram.service`, затем `nevidimy-agent.service`.
Не запускай второй poller. Ядро Chatwoot пока нужно для точечной смены URL в БД.
Найди его контейнер по labels project/service, а не по старому имени Podman.

Через **ещё работающий** Chatwoot выполни Rails runner. Значение URL берётся
из env контейнера; токен и URL не попадают в аргументы или вывод. Например,
после выбора `nevidimy_chatwoot_container` на VM:

```bash
docker exec "$nevidimy_chatwoot_container" bundle exec rails runner '
begin
  ActiveRecord::Base.logger = nil
  old_url = ENV.fetch("NEVIDIMY_AGENT_WEBHOOK_URL")
  raise "unexpected route" unless old_url.match?(%r{\Ahttps?://agent-bot:8080/webhooks/chatwoot/agent/[A-Za-z0-9_-]{32,128}\z})
  new_url = old_url.sub(/\Ahttp:/, "https:")
  AgentBot.transaction do
    matches = AgentBot.where(outgoing_url: [old_url, new_url]).lock.to_a
    raise "unexpected bot count" unless matches.length == 1
    matches.first.update!(outgoing_url: new_url)
  end
  puts "agent_webhook_https=true"
rescue => error
  warn "webhook_migration_failed=" + error.class.name
  exit 1
end'
```

При неожиданном числе Agent Bot не выбирай произвольную запись. Останови
переключение и сверь существующую связь. Если env контейнера отличается от
закрытого env-файла, сначала устрани расхождение с сохранением исходного URL.

После успешного обновления БД останови `nevidimy.service`. В закрытом
`/etc/women-help-chatwoot.env` атомарно обнови **только**:

```dotenv
CHATWOOT_INTERNAL_TLS=1
INTERNAL_TLS_DIR=/opt/nevidimy/certs/internal-tls
POSTGRES_TLS_CA_FILE=/opt/nevidimy/certs/postgresql-ca.crt
```

И переведи существующий `NEVIDIMY_AGENT_WEBHOOK_URL` с `http://` на `https://`,
сохранив весь остальной URL. Используй локальный root Python-процесс, не `sed`
с секретом в командной строке; файл должен остаться root-owned `0600`.
Не трогай HMAC-секрет, credentials, закреплённый образ Chatwoot, провайдера LLM,
S3 и настройки managed-БД. Не включай `SAFE_FETCH_ALLOW_PRIVATE_NETWORK=true`.

## 6. Переключить код и запустить сервисы

Всё далее выполняется на проверенной VM:

1. Переключи `/opt/nevidimy/app` на новый release directory. Если это обычный
   каталог, сначала перемести его целиком в backup; если ссылка — сохрани прежний
   target. Не удаляй старый релиз и не копируй новый код поверх него.
2. Перетегируй уже доставленный новый Python image в
   `localhost/women-help-chatwoot-agent:current`. Старый image и rollback-тег
   должны остаться доступными. Образ Chatwoot не менять.
3. Из нового релиза установи `deploy/timeweb/nevidimy.service`,
   `nevidimy-agent.service`, `nevidimy-telegram.service` в `/etc/systemd/system/`.
   Обнови **существующий** `nevidimy.service.d/http-local.conf` из
   `deploy/timeweb/http-local.conf`; для действующего общего HTTPS также обнови
   `public-domain-https.conf` из `deploy/timeweb/domain-https.conf`.
   Сохрани остальные drop-ins. SSH proxy-tunnel
   unit не меняется.
4. До запуска проверь:

```bash
cd /opt/nevidimy/app
python3 scripts/chatwoot_compose.py config --quiet
systemctl daemon-reload
systemctl start nevidimy.service
systemctl start nevidimy-agent.service
systemctl start nevidimy-telegram.service
```

Запускай только сервисы, которые были активны до обслуживания. В записанном
Timeweb-контуре все перечисленные сервисы активны; если это уже не так,
сохраняй фактический выбор. `nevidimy-proxy-tunnel.service` не требуется
перезапускать, если он остаётся active. `certificate-admin-bot`, если его
включили позднее, тоже требует пересоздания с новым образом и TLS-настройками.

Не выполнять миграции новой версии Chatwoot: этот релиз меняет транспорт и
Python-код, а не закреплённую версию Chatwoot или схему его БД.

## 7. Живая приёмка

Проверяй через общий `python3 scripts/chatwoot_compose.py`, чтобы ручные команды
и systemd использовали один набор overlays. Не выводи полный `docker inspect`,
env или реальные сообщения пользователей.

- Все ранее активные units active, контейнеры с healthcheck — healthy.
  `exec -T chatwoot ruby /run/tls-healthcheck.rb` должен завершиться успешно.
- В Rails и Sidekiq настоящий `pg_stat_ssl` для текущего backend PID показывает
  `ssl=true`; Redis PING проходит через их штатный `rediss://` клиент.
- Из backend и ingress проверенный HTTPS к `https://chatwoot:3000/api` работает.
  Из Python SQLAlchemy/asyncpg-пути приложения подключение к managed-БД
  сохраняет `verify-full` и `pg_stat_ssl.ssl=true`.
- После успешного TLS PING обычный `redis-cli` без `--tls` должен получить
  отказ. HTTP к Chatwoot:3000 и backend:8080 должен отклоняться при работающих
  HTTPS healthchecks. Не исправляй ошибки отключением проверки CA/имени.
- В локальном режиме через Mac SSH-туннель панель `http://127.0.0.1:18080` открывается, Caddy
  подключается к Chatwoot по TLS. На VM опубликован только loopback 18080;
  внутренние 3000/8080/6379 наружу не доступны. С Mac проверь закрытость
  VM:80/443/18080/3000/8080/6379. Для действующего общего HTTPS вместо этого проверь
  настоящий публичный сертификат/имя, HTTP-редирект и доступность страницы входа;
  опубликованы только Caddy:80/443, не 18080/3000/8080/6379.
  Отдельно сверь firewall managed-БД: 5432
  разрешён только ожидаемой VM, проверка с Mac не должна подключаться.
- Проверь нативный подписанный webhook в изолированном **Channel::Api** inbox
  без внешней доставки: синтетическое сообщение → backend → ответ в Chatwoot,
  передача человеку и молчание бота, возврат боту. Подходящий существующий
  приёмщик — `scripts/chatwoot_regression_acceptance.py --mode webhook
  --inbox-name <точное согласованное имя API inbox> --output <закрытый отчёт>`.
  Не направляй его в Telegram inbox и не используй реальную переписку.
- Для Telegram проверь восстановление ровно одного существующего polling
  без смены выбранного бота, токена и курсора. Не повторяй переключение на
  `@nevidima_bot` или обратно на MVP в рамках обновления. Реальные
  сообщения третьим лицам не отправлять.

Только после живой проверки обнови Mermaid-подписи в `docs/architecture.md`
на HTTPS/Redis TLS и перерендери `architecture-2.svg/png`, сохранив стили,
блоки и исходный `architecture-1`. Managed PostgreSQL TLS уже указан верно.

## 8. Откат и завершение

Если запуск/приёмка неуспешны, останови новые telegram, agent и core, восстанови
прежние env, units/drop-ins, `/opt/nevidimy/app` и Python image по сохранённым
ID/rollback-тегам. Восстанови только нужный `AgentBot.outgoing_url` из закрытой
резервной копии исходного env, без вывода URL. Эта обратная точечная операция
может выполняться Rails runner старого core, пока доставки и backend остановлены;
после неё перезапусти core, чтобы его env и запись БД снова совпадали.
После `daemon-reload` запускай только ранее активные сервисы. Дампы БД не
восстанавливать автоматически поверх сообщений, появившихся после backup.
Redis и остальные volumes не удалять; не очищать очередь для устранения ошибки.

После успеха сохрани закрытый deployment report: commit, image IDs, версия
Chatwoot, путь backup, статусы проверок и фактические открытые порты. Без токенов,
URL с секретным путём, приватных ключей и содержимого диалогов.

Leaf-сертификаты действуют 90 дней. Обновление —
`python3 -m scripts.prepare_internal_tls --output-dir /opt/nevidimy/certs/internal-tls --renew`,
затем согласованное пересоздание затронутых контейнеров. CA и его закрытый ключ
сохраняются на VM; в контейнеры CA key не монтируется.

Отменённый облачный доступ больше не нужен: через рабочий Mac SSH удали только
запись с fingerprint `SHA256:qW9K68eAODqrQilV9qzPexa/88Eqs1rOtRb1hibynU0`
из `authorized_keys`, сохранив остальные ключи. Пробный Mac-ключ `qq+fj...`
по последнему отчёту уже удалён; персональные ключи не трогать.
