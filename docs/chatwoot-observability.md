# Метрики запросов Chatwoot

Backend экспортирует Prometheus-метрики по `/metrics` на том же **внутреннем
HTTPS-порту**, что webhook. Отдельный HTTP-сервер или публичный порт не нужен.
Внешний reverse proxy Chatwoot этот адрес не публикует. В режиме без внутреннего
TLS endpoint нельзя выставлять наружу.

Используется официальный `prometheus-client`; зависимости закреплены в `uv.lock`.
Метрики живут в памяти одного процесса и сбрасываются при его замене. Для истории
нужен Prometheus, для графиков — Grafana. Наличие `/metrics` само по себе не означает,
что сборщик и дашборд уже запущены.

## Что измеряется

| Семейство | Что означает |
| --- | --- |
| `nevidimy_chatwoot_api_request_duration_seconds` | Все исходящие запросы к Chatwoot, включая служебные события и планировщик |
| `nevidimy_http_request_duration_seconds` | Входящие HTTP-запросы backend: webhook, health и прочие; не полное время ответа бота |
| `nevidimy_event_processing_duration_seconds` | Обработка одной попытки входящего сообщения; результат `replied`, `silent` или `error` |
| `nevidimy_event_queue_age_seconds` | Возраст входящего события Redis в начале попытки; включает ожидание повторов |
| `nevidimy_event_stage_duration_seconds` | История, сценарий, проверки владения, запись состояния и ответа |
| `nevidimy_first_reply_created_seconds` | До записи первого публичного ответа, начиная с обработки; **без доставки Telegram** |
| `nevidimy_event_api_requests` | Число исходящих запросов к Chatwoot в одной попытке обработки |

Суффикс `_count` даёт число наблюдений, `_sum` — сумму, `_bucket` — распределение.
Количество запросов в секунду вычисляется через `rate(..._count[5m])`; отдельные
дублирующие счётчики для тех же наблюдений не нужны.

### Быстрые сценарные кнопки

`CHATWOOT_FAST_BUTTONS` включает компактное чтение и условную атомарную запись
на существующих авторизованных API-действиях Chatwoot. Python рассчитывает тот
же сценарий в буфере без внешних эффектов. Chatwoot под блокировкой повторяет
проверки версии, назначения специалистке, позднего ответа и дубликата, затем
сохраняет workflow и исходящий Message одной транзакцией. Только явно разрешённые
внутренние поля workflow не порождают повторный `conversation_updated`.

При объявленной native capability `private_notes_v1` в ту же транзакцию входят
сценарные заявки, их receipts и до четырёх приватных заметок с ключами
`scenario:<24 hex>` / `scenario-alert:<24 hex>`. Существующие receipts читаются
через native coder во всём разговоре, включая записи до текущего входящего.
Повтор или сбой создания любой заметки/ответа не оставляет частичных записей.
Без этой capability план с заметками возвращается к прежнему обработчику до POST.

Файлы, уведомления, смена назначения и другие неподдерживаемые эффекты переходят
на прежний обработчик **до** внешних действий. Неизвестный результат POST не
считается разрешением отправить ещё один ответ. Нативные callbacks доставки
остаются после commit: сохранённый Message сам по себе не доказывает Telegram-доставку.

Ключ ответа читается через native `store` coder Chatwoot: установленная версия
хранит `content_attributes` как JSON-строку внутри jsonb, поэтому прямой поиск
JSON-ключа может пропустить дубликат. Проверяется только исходящая метаинформация
после входящего ID, без загрузки текста истории.

`tests/timeweb_atomic_turn_live_probe.rb` проверяет native авторизацию, откат,
дубликаты и настоящие блокировки БД. Запускать только с подготовленным уникальным
API-inbox и acceptance-маркером, никогда на клиентском разговоре. Реквизиты
передаются через закрытое окружение. Задания перехватывает тестовый адаптер:
такой прогон не подтверждает скорость production или доставку Telegram.

Лог `nevidimy_atomic_turn_timing` разделяет native builder, after-commit callbacks
и SQL (`schema`, `cached`, `uncached`) внутри atomic-create. В нём только
фиксированные числовые поля, HTTP-статус и ID исходящего сообщения; SQL, binds,
текст и параметры не записываются. Это время тела action без предшествующей
авторизации и сети, а не end-to-end latency. Вложенные spans нельзя слепо суммировать.

Нативный Telegram sender переиспользует проверенные HTTPS-соединения через
существующий прокси, без автоматического повтора неоднозначного POST. Его
content-free лог `nevidimy_telegram_send` отдельно показывает `http_elapsed_ms`,
`send_elapsed_ms` и `source_to_accepted_ms`. Последняя величина начинается до
подтверждения callback в ingress и заканчивается получением внешнего message ID;
это не время отображения на телефоне. Без доверенной входящей метки она `null`.

Целевые 200 мс необходимо подтверждать живыми измерениями этой границы, отдельно
для холодного/повторно используемого соединения и разных действий кнопок.
Локальный тест буфера, низкая задержка ack и время записи Message этого не доказывают.

Метки ограничены типом события, этапом, нормализованной операцией, HTTP-методом,
классом статуса и результатом. В них нет текста, URL, параметров, токенов, email,
ID разговора, сообщения или request ID. Неизвестные значения объединяются в
`other`. Коды без HTTP-ответа попадают в `status_class="error"`.
Метрики не вызывают LLM и не делают дополнительных сетевых запросов.

## Как подключить на Timeweb

У Timeweb есть [мониторинг ресурсов VM](https://timeweb.cloud/docs/cloud-servers/manage-servers/resource-stats)
и [проверки доступности](https://timeweb.cloud/docs/monitoring/types), но это не
гистограммы наших прикладных запросов. Для managed PostgreSQL Timeweb предоставляет
[Prometheus Exporter](https://timeweb.cloud/docs/dbaas/prometheus-exporter):
доступ зависит от сети BGP; для каждого узла репликации нужны собственные targets.
Фактическую доступность на кластере `4221087` нужно проверить перед подключением.
Пароли экспортера нельзя записывать в Git.

Для нашего Docker Compose-контура следующий шаг — закрытый Prometheus и Grafana.
Kubernetes ради мониторинга не нужен. До запуска проверь свободную RAM/диск,
задай лимиты ресурсов и хранения. Новые платные ресурсы, публичный доступ и новый
HTTPS-hostname требуют соответствующего согласования. Не открывай `/metrics`,
9090 и Grafana без контроля доступа.

Пример scrape-конфига для Prometheus **в той же Docker-сети**:

```yaml
scrape_configs:
  - job_name: nevidimy-agent
    scrape_interval: 15s
    scheme: https
    metrics_path: /metrics
    static_configs:
      - targets: [agent-bot:8080]
    tls_config:
      ca_file: /run/tls/ca.crt
      server_name: agent-bot
```

CA нужно смонтировать read-only из действующей конфигурации внутреннего TLS.
Не используй `insecure_skip_verify`. На произвольном внешнем сервере hostname
`agent-bot` не разрешается: пример не создаёт сетевой доступ автоматически.

## Запросы для Grafana

Медиана и p95 времени до создания ответа для кнопок (замени `0.95` на `0.5`):

```promql
histogram_quantile(0.95,
  sum by (le) (rate(nevidimy_first_reply_created_seconds_bucket{kind="callback"}[15m])))
```

Длительность исходящих запросов по операциям:

```promql
histogram_quantile(0.95,
  sum by (le, operation) (rate(nevidimy_chatwoot_api_request_duration_seconds_bucket[15m])))
```

Число запросов за интервал и доля серверных/транспортных ошибок:

```promql
sum by (operation, status_class) (increase(nevidimy_chatwoot_api_request_duration_seconds_count[15m]))

sum(rate(nevidimy_chatwoot_api_request_duration_seconds_count{status_class=~"5xx|error"}[15m]))
/
sum(rate(nevidimy_chatwoot_api_request_duration_seconds_count[15m]))
```

Среднее число API-запросов на нажатие:

```promql
sum(rate(nevidimy_event_api_requests_sum{kind="callback"}[15m]))
/
sum(rate(nevidimy_event_api_requests_count{kind="callback"}[15m]))
```

На графиках показывай и размер выборки. При отсутствии запросов доля ошибок и
квантили могут быть `NaN`; это не 0% ошибок и не нулевая задержка. Гистограммные
квантили — оценка внутри бакетов, а не точные квантили отдельных событий.
Для точного разбора одного события остаются content-free логи с request ID.
