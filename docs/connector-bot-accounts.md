# nevidima_bot и nevidima_mvp_bot в ВК и WhatsApp

Это четыре отдельных подключения к Chatwoot. Имена обозначают рабочую и
тестовую версии проекта; запись профиля на диске не регистрирует аккаунт
провайдера и не подтверждает доступность адреса сообщества.

| Профиль | Что нужно на платформе | Compose project | Локальный порт |
| --- | --- | --- | --- |
| `vk-nevidima_bot` | Рабочее сообщество фонда, можно существующее | `vk-nevidima-bot` | 8091 |
| `vk-nevidima_mvp_bot` | Отдельное тестовое сообщество | `vk-nevidima-mvp-bot` | 8092 |
| `whatsapp-nevidima_bot` | Рабочий номер фонда в WhatsApp Cloud API | `whatsapp-nevidima-bot` | 8093 |
| `whatsapp-nevidima_mvp_bot` | Тестовый номер Meta или отдельный тестовый номер | `whatsapp-nevidima-mvp-bot` | 8094 |

У каждого подключения собственные API inbox, служебный пользователь Chatwoot,
env-файл, callback secret, образ, Redis и том. Один и тот же VK group ID или
WhatsApp Phone Number ID нельзя использовать одновременно для рабочей и
MVP-версии. При необходимости обе версии WhatsApp могут находиться в одной
WABA, но номера и inbox должны быть разными.

## Что означает «создать бота»

В **ВК** бот отвечает от имени сообщества — группы или публичной страницы.
Для рабочего канала подходит уже существующая страница фонда. Для тестового
создаётся отдельное сообщество, например «Невидимый фонд — тестовый бот».
Желаемые короткие адреса `nevidima_bot` и `nevidima_mvp_bot` можно назначить,
если они свободны. Подключение требует прав администратора, включённых сообщений
сообщества, community access token для сообщений и документов, Callback API
secret и confirmation code. Коннектор не использует пароль личной страницы.

В **WhatsApp** Cloud API отправляет сообщения с телефонного номера бизнеса.
Для рабочего канала нужен контролируемый фондом номер и возможность пройти
подтверждение владения. Для MVP можно начать с тестового номера из панели Meta:
отдельную SIM-карту для первого теста покупать не требуется. Тестовый номер
предназначен для добавленных тестовых получателей; он не заменяет рабочий канал.
Имена профилей проекта не резервируют WhatsApp username или business display name.

Владелец входит в Meta for Developers, создаёт приложение с WhatsApp Cloud API
и получает WABA ID, Phone Number ID, app secret и access token. Для живого теста
он добавляет номер тестировщика и подтверждает его в интерфейсе Meta.
Регистрация аккаунтов, вход, SMS-коды и подтверждение владения выполняются в
собственном аккаунте владельца; подготовленные локальные файлы этого не делают.
Значения токенов сохраняются в приватных env-файлах, не в чате и не в Git.

## Подготовленные профили

```bash
uv run python -m scripts.connector_profiles init \
  --directory .runtime/connector-profiles \
  --chatwoot-url https://chatwoot.example.org \
  --webhook-domain bots.example.org \
  --image-version RELEASE

just connector-profiles-status
uv run python -m scripts.connector_profiles check
```

Адреса `example.org` в примере нужно заменить собственными. Если домены ещё не
выбраны, `init` можно вызвать без них: отчёт честно покажет недостающие поля.
Каталог создаётся с правами 0700, файлы — 0600. Повторный `init` отказывается
перезаписывать секреты. Генерируются независимые случайные callback/VK/verify
secrets; провайдерские токены, номера и inbox ID не подставляются.
`status` печатает только названия профилей и полей. `check` возвращает код 2,
пока хотя бы один профиль не заполнен или обнаружено пересечение каналов.

Проверка обнаруживает повторное использование внешнего ID, Chatwoot inbox,
Chatwoot API token, inbox identifier/HMAC token, callback secret, публичного
адреса или порта. Файловая проверка не подхватывает недостающие секреты MVP
из process environment рабочей версии и не обращается к провайдерам.
Она проверяет конфигурацию, а не действительность доступа к аккаунтам.

Создать четыре API inbox с именами из поля `CONNECTOR_CHATWOOT_INBOX_NAME`,
подключить общего Agent Bot и заполнить реквизиты в соответствующих файлах.
`CONNECTOR_PUBLIC_URL` — origin выделенного HTTPS endpoint; адрес API inbox
callback получается добавлением `/webhooks/chatwoot/` и его callback secret.
Для ВК входящий URL заканчивается `/webhooks/vk`, для WhatsApp —
`/webhooks/whatsapp`. Эти секретные callback URL не печатать в общие логи.

При запуске каждого проекта использовать его имя из таблицы:

```bash
docker compose -p vk-nevidima-mvp-bot \
  --env-file .runtime/connector-profiles/vk-nevidima_mvp_bot.env \
  -f deploy/connectors/compose.yml up -d --build
```

Схема транспорта, подтверждение PDF и ограничения WhatsApp описаны в
[основной инструкции](messenger-connectors.md).

## Навыки и инструменты

Найдены [Meta Agentic Tools](https://github.com/facebook/agentic-tools):
`api-integration`, `webhook-setup`, `debug-webhooks`, `debug-access-token`.
Это проект Meta для настройки приложений и API; навыки работают через Meta
Social Technologies MCP и требуют OAuth-подключения аккаунта. В текущей среде
этот MCP не подключён, поэтому наличие навыков не даёт доступа к WABA.

Также найден сторонний [WhatsApp Cloud API skill](https://github.com/bellopushon/whatsapp-cloud-api)
с материалами по API, шаблонам, медиа и webhook. Он не регистрирует бизнес и
телефонные номера. Эти внешние навыки не установлены и не запускались.
Подходящего готового навыка регистрации VK communities при поиске не найдено.

Описание ботов со стороны ВК: [материал команды VK](https://habr.com/ru/companies/vk/articles/570486/).
Пример настройки тестового получателя WhatsApp:
[Postman: Send a Test Message](https://www.postman.com/postman/brewing-postman-flows/folder/j1s1ewt/step-2-send-a-test-message).

## Проверка

`just test-connector-profiles` проверяет четыре профиля и границу production/MVP:
свой входящий webhook и callback принимаются, чужой group/phone ID, inbox и
callback secret не создают задания; потерянный секрет MVP не берётся из рабочего
окружения. Провайдерские тесты и набор Chatwoot запускаются отдельно.
Эти проверки используют синтетические сообщения, не зарегистрированные
сообщества и номера. Живая приёмка возможна после подключения аккаунтов.
