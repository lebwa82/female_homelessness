# Проверка локальной реализации DDS — 27 ноября 2026

Production, настройки Timeweb, сервисы бота, доступы аналитиков и расписание не менялись. Git-публикация кода сама по себе их не активирует.

## Успешные проверки

- 85 тестов DDS с отдельным локальным PostgreSQL14.11: схема/COMMENT,105 mapping, read-only source, scope двух аккаунтов, семь фактов, ключи/NULL/даты, первая и инкрементальная загрузка, повтор, полная сверка, удаления, откат, история/checkpoint, десять SQL-примеров и ACL.
- Подключение источника реальной CLI-проверки выполнялось отдельной локальной ролью SELECT-only. Команды validate, migrate --apply, load --apply --full, load --apply и status прошли. Фикстуры синтетические, БД и ключ удалены после проверки.
- Каталог:19 таблиц и105 требований. PostgreSQL COMMENT синхронны с каталогом. Ruff и форматирование новых модулей/тестов проходят, git diff --check чист.
- Независимое ревью: найденные P1 исправлены, финальные целевые проверки sender/ACL/полной сверки проходят; открытых P1/P2 по проверенному локальному объёму не осталось.

## Полный набор проекта

Итог последнего полного запуска: 20 failed, 2756 passed, 70 skipped, 5 warnings in 42.68s.
Все20 падений воспроизведены на чистом архиве исходного commit1d32c9d без DDS. Наборы упавших test ID совпадают. Существующие тесты Compose/телеметрии не исправлялись в рамках DDS. Пропуски и предупреждения полного набора не являются проверкой production.

- `tests/test_chatwoot_deploy_files.py::test_agent_bot_uses_chatwoot_database_without_a_persistent_volume`
- `tests/test_chatwoot_ingress_timing.py::test_sender_telemetry_has_honest_boundaries_and_never_logs_content_or_errors`
- `tests/test_chatwoot_managed_compose.py::test_default_compose_keeps_local_postgres`
- `tests/test_chatwoot_managed_compose.py::test_http_local_publishes_only_loopback_and_keeps_database_tls`
- `tests/test_chatwoot_managed_compose.py::test_managed_compose_fails_closed_when_database_host_is_missing`
- `tests/test_chatwoot_managed_compose.py::test_managed_compose_removes_local_postgres_and_shares_database`
- `tests/test_chatwoot_managed_compose.py::test_prompt_storage_is_shared_persistent_and_backend_read_only`
- `tests/test_chatwoot_managed_compose.py::test_timeweb_certificate_admin_reuses_existing_telegram_proxy`
- `tests/test_chatwoot_managed_compose.py::test_timeweb_domain_https_exposes_only_chatwoot`
- `tests/test_chatwoot_managed_compose.py::test_timeweb_ip_https_publishes_only_tls_proxy_and_preserves_internal_services`
- `tests/test_chatwoot_managed_compose.py::test_timeweb_ip_https_requires_current_ip_and_automates_public_certificates`
- `tests/test_chatwoot_managed_compose.py::test_timeweb_mounts_ca_and_does_not_enable_telegram`
- `tests/test_chatwoot_managed_compose.py::test_timeweb_telegram_is_explicit_and_uses_minimal_credentials`
- `tests/test_chatwoot_managed_compose.py::test_timeweb_vk_callback_only_shares_caddy_network[chatwoot.example.test-routes]`
- `tests/test_chatwoot_managed_compose.py::test_timeweb_vk_callback_only_shares_caddy_network[vk.example.test-sites]`
- `tests/test_internal_tls.py::test_compose_tls_is_explicit_verified_and_does_not_publish_internal_ports[False-False-False-False]`
- `tests/test_internal_tls.py::test_compose_tls_is_explicit_verified_and_does_not_publish_internal_ports[True-False-False-False]`
- `tests/test_internal_tls.py::test_compose_tls_is_explicit_verified_and_does_not_publish_internal_ports[True-True-False-False]`
- `tests/test_internal_tls.py::test_compose_tls_is_explicit_verified_and_does_not_publish_internal_ports[True-True-False-True]`
- `tests/test_internal_tls.py::test_compose_tls_is_explicit_verified_and_does_not_publish_internal_ports[True-True-True-False]`

## Ограничения и следующий этап

Живая PG18 и реальные данные этим этапом не тестировались; использован PostgreSQL14.11 на отдельном loopback-порту. Перед production заново проверить фактическую схему, scope/test exclusions, резервную копию, доступы, эксплуатационные лимиты и план пилота. Подготовленные access.sql и service/timer.example не применялись.
