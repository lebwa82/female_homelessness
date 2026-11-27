CREATE SCHEMA IF NOT EXISTS dds;

CREATE TABLE IF NOT EXISTS dds.dim_contact (
    account_id bigint NOT NULL,
    contact_key uuid NOT NULL,
    created_at timestamptz,
    updated_at timestamptz,
    row_hash text NOT NULL,
    loaded_at timestamptz NOT NULL,
    deleted_at timestamptz,
    PRIMARY KEY (contact_key)
);

COMMENT ON TABLE dds.dim_contact IS 'Одна карточка контакта в аккаунте Chatwoot. Источник — contacts.';

COMMENT ON COLUMN dds.dim_contact.account_id IS 'Аккаунт Chatwoot, которому принадлежит строка DDS. NULL недопустим.';

COMMENT ON COLUMN dds.dim_contact.contact_key IS 'ID контакта. NULL недопустим.';

COMMENT ON COLUMN dds.dim_contact.created_at IS 'Дата создания карточки контакта. Момент первого обращения за помощью неизвестен. NULL означает отсутствие доступного корректного значения.';

COMMENT ON COLUMN dds.dim_contact.updated_at IS 'Дата последнего изменения карточки. NULL означает отсутствие доступного корректного значения.';

COMMENT ON COLUMN dds.dim_contact.row_hash IS 'Хеш выбранных значений строки DDS для определения изменений.';

COMMENT ON COLUMN dds.dim_contact.loaded_at IS 'Момент последней записи версии строки в DDS.';

COMMENT ON COLUMN dds.dim_contact.deleted_at IS 'Момент обнаружения исчезновения из выбранного среза (удаление или отзыв видимости). NULL у активной строки.';

CREATE TABLE IF NOT EXISTS dds.dim_inbox (
    account_id bigint NOT NULL,
    inbox_key uuid NOT NULL,
    channel_type text,
    csat_survey_enabled boolean,
    lock_to_single_conversation boolean,
    updated_at timestamptz,
    row_hash text NOT NULL,
    loaded_at timestamptz NOT NULL,
    deleted_at timestamptz,
    PRIMARY KEY (inbox_key)
);

COMMENT ON TABLE dds.dim_inbox IS 'Один включённый в область загрузки канал аккаунта. Источник — inboxes.';

COMMENT ON COLUMN dds.dim_inbox.account_id IS 'Аккаунт Chatwoot, которому принадлежит строка DDS. NULL недопустим.';

COMMENT ON COLUMN dds.dim_inbox.inbox_key IS 'ID канала Chatwoot. NULL недопустим.';

COMMENT ON COLUMN dds.dim_inbox.channel_type IS 'Тип канала связи. NULL означает отсутствие доступного корректного значения.';

COMMENT ON COLUMN dds.dim_inbox.csat_survey_enabled IS 'Включён ли опрос CSAT в канале. NULL означает отсутствие доступного корректного значения.';

COMMENT ON COLUMN dds.dim_inbox.lock_to_single_conversation IS 'Включено ли повторное использование одного диалога. NULL означает отсутствие доступного корректного значения.';

COMMENT ON COLUMN dds.dim_inbox.updated_at IS 'Дата изменения настроек канала. NULL означает отсутствие доступного корректного значения.';

COMMENT ON COLUMN dds.dim_inbox.row_hash IS 'Хеш выбранных значений строки DDS для определения изменений.';

COMMENT ON COLUMN dds.dim_inbox.loaded_at IS 'Момент последней записи версии строки в DDS.';

COMMENT ON COLUMN dds.dim_inbox.deleted_at IS 'Момент обнаружения исчезновения из выбранного среза (удаление или отзыв видимости). NULL у активной строки.';

CREATE TABLE IF NOT EXISTS dds.dim_staff (
    account_id bigint NOT NULL,
    staff_key uuid NOT NULL,
    account_type text,
    updated_at timestamptz,
    row_hash text NOT NULL,
    loaded_at timestamptz NOT NULL,
    deleted_at timestamptz,
    PRIMARY KEY (staff_key)
);

COMMENT ON TABLE dds.dim_staff IS 'Одна учётная запись назначенного или связанного пользователя. Источник — users.';

COMMENT ON COLUMN dds.dim_staff.account_id IS 'Аккаунт Chatwoot, которому принадлежит строка DDS. NULL недопустим.';

COMMENT ON COLUMN dds.dim_staff.staff_key IS 'ID учётной записи автора или назначенного специалиста. NULL недопустим.';

COMMENT ON COLUMN dds.dim_staff.account_type IS 'Тип учётной записи. NULL часто встречается в источнике. Тип сам по себе не подтверждает ручное авторство. NULL означает отсутствие доступного корректного значения.';

COMMENT ON COLUMN dds.dim_staff.updated_at IS 'Дата изменения учётной записи. NULL означает отсутствие доступного корректного значения.';

COMMENT ON COLUMN dds.dim_staff.row_hash IS 'Хеш выбранных значений строки DDS для определения изменений.';

COMMENT ON COLUMN dds.dim_staff.loaded_at IS 'Момент последней записи версии строки в DDS.';

COMMENT ON COLUMN dds.dim_staff.deleted_at IS 'Момент обнаружения исчезновения из выбранного среза (удаление или отзыв видимости). NULL у активной строки.';

CREATE TABLE IF NOT EXISTS dds.dim_aid (
    account_id bigint NOT NULL,
    aid_id text NOT NULL,
    row_hash text NOT NULL,
    loaded_at timestamptz NOT NULL,
    deleted_at timestamptz,
    PRIMARY KEY (account_id, aid_id)
);

COMMENT ON TABLE dds.dim_aid IS 'Один разрешённый код вида помощи, наблюдавшийся в строках DDS аккаунта. Источник — Коды видов помощи из фактов и моста применимости пулов DDS.';

COMMENT ON COLUMN dds.dim_aid.account_id IS 'Аккаунт Chatwoot, которому принадлежит строка DDS. NULL недопустим.';

COMMENT ON COLUMN dds.dim_aid.aid_id IS 'Разрешённый код вида помощи. NULL недопустим.';

COMMENT ON COLUMN dds.dim_aid.row_hash IS 'Хеш выбранных значений строки DDS для определения изменений.';

COMMENT ON COLUMN dds.dim_aid.loaded_at IS 'Момент последней записи версии строки в DDS.';

COMMENT ON COLUMN dds.dim_aid.deleted_at IS 'Момент обнаружения исчезновения из выбранного среза (удаление или отзыв видимости). NULL у активной строки.';

CREATE TABLE IF NOT EXISTS dds.dim_certificate_pool (
    account_id bigint NOT NULL,
    pool_key uuid NOT NULL,
    provider text,
    row_hash text NOT NULL,
    loaded_at timestamptz NOT NULL,
    deleted_at timestamptz,
    PRIMARY KEY (pool_key)
);

COMMENT ON TABLE dds.dim_certificate_pool IS 'Один пул, связанный с отобранной записью сертификата или очереди. Источник — women_help_certificate_pools.';

COMMENT ON COLUMN dds.dim_certificate_pool.account_id IS 'Аккаунт Chatwoot, которому принадлежит строка DDS. NULL недопустим.';

COMMENT ON COLUMN dds.dim_certificate_pool.pool_key IS 'ID пула сертификатов. NULL недопустим.';

COMMENT ON COLUMN dds.dim_certificate_pool.provider IS 'Провайдер пула, связанного с отобранной записью сертификата или очереди. NULL означает отсутствие доступного корректного значения.';

COMMENT ON COLUMN dds.dim_certificate_pool.row_hash IS 'Хеш выбранных значений строки DDS для определения изменений.';

COMMENT ON COLUMN dds.dim_certificate_pool.loaded_at IS 'Момент последней записи версии строки в DDS.';

COMMENT ON COLUMN dds.dim_certificate_pool.deleted_at IS 'Момент обнаружения исчезновения из выбранного среза (удаление или отзыв видимости). NULL у активной строки.';

CREATE TABLE IF NOT EXISTS dds.dim_label (
    account_id bigint NOT NULL,
    label_key uuid NOT NULL,
    title text,
    updated_at timestamptz,
    row_hash text NOT NULL,
    loaded_at timestamptz NOT NULL,
    deleted_at timestamptz,
    PRIMARY KEY (label_key)
);

COMMENT ON TABLE dds.dim_label IS 'Одна метка Chatwoot из согласованного списка названий. Источник — labels.';

COMMENT ON COLUMN dds.dim_label.account_id IS 'Аккаунт Chatwoot, которому принадлежит строка DDS. NULL недопустим.';

COMMENT ON COLUMN dds.dim_label.label_key IS 'ID метки Chatwoot. NULL недопустим.';

COMMENT ON COLUMN dds.dim_label.title IS 'Название метки из согласованного списка категорий. NULL означает отсутствие доступного корректного значения.';

COMMENT ON COLUMN dds.dim_label.updated_at IS 'Дата изменения метки. NULL означает отсутствие доступного корректного значения.';

COMMENT ON COLUMN dds.dim_label.row_hash IS 'Хеш выбранных значений строки DDS для определения изменений.';

COMMENT ON COLUMN dds.dim_label.loaded_at IS 'Момент последней записи версии строки в DDS.';

COMMENT ON COLUMN dds.dim_label.deleted_at IS 'Момент обнаружения исчезновения из выбранного среза (удаление или отзыв видимости). NULL у активной строки.';

CREATE TABLE IF NOT EXISTS dds.dim_tag (
    account_id bigint NOT NULL,
    tag_key uuid NOT NULL,
    name text,
    row_hash text NOT NULL,
    loaded_at timestamptz NOT NULL,
    deleted_at timestamptz,
    PRIMARY KEY (tag_key)
);

COMMENT ON TABLE dds.dim_tag IS 'Один тег из согласованного списка названий в области аккаунта. Источник — tags.';

COMMENT ON COLUMN dds.dim_tag.account_id IS 'Аккаунт Chatwoot, которому принадлежит строка DDS. NULL недопустим.';

COMMENT ON COLUMN dds.dim_tag.tag_key IS 'ID тега в хранилище тегов. NULL недопустим.';

COMMENT ON COLUMN dds.dim_tag.name IS 'Название тега из согласованного списка категорий. NULL означает отсутствие доступного корректного значения.';

COMMENT ON COLUMN dds.dim_tag.row_hash IS 'Хеш выбранных значений строки DDS для определения изменений.';

COMMENT ON COLUMN dds.dim_tag.loaded_at IS 'Момент последней записи версии строки в DDS.';

COMMENT ON COLUMN dds.dim_tag.deleted_at IS 'Момент обнаружения исчезновения из выбранного среза (удаление или отзыв видимости). NULL у активной строки.';

CREATE TABLE IF NOT EXISTS dds.bridge_contact_inbox (
    account_id bigint NOT NULL,
    contact_inbox_key uuid NOT NULL,
    contact_key uuid,
    inbox_key uuid,
    created_at timestamptz,
    updated_at timestamptz,
    row_hash text NOT NULL,
    loaded_at timestamptz NOT NULL,
    deleted_at timestamptz,
    PRIMARY KEY (contact_inbox_key)
);

COMMENT ON TABLE dds.bridge_contact_inbox IS 'Одна запись связи контакта с каналом. Источник — contact_inboxes.';

COMMENT ON COLUMN dds.bridge_contact_inbox.account_id IS 'Аккаунт Chatwoot, которому принадлежит строка DDS. NULL недопустим.';

COMMENT ON COLUMN dds.bridge_contact_inbox.contact_inbox_key IS 'ID связи контакта с каналом. NULL недопустим.';

COMMENT ON COLUMN dds.bridge_contact_inbox.contact_key IS 'ID контакта. NULL означает отсутствие доступного корректного значения.';

COMMENT ON COLUMN dds.bridge_contact_inbox.inbox_key IS 'ID канала. NULL означает отсутствие доступного корректного значения.';

COMMENT ON COLUMN dds.bridge_contact_inbox.created_at IS 'Дата создания связи контакта с каналом. NULL означает отсутствие доступного корректного значения.';

COMMENT ON COLUMN dds.bridge_contact_inbox.updated_at IS 'Дата изменения связи. NULL означает отсутствие доступного корректного значения.';

COMMENT ON COLUMN dds.bridge_contact_inbox.row_hash IS 'Хеш выбранных значений строки DDS для определения изменений.';

COMMENT ON COLUMN dds.bridge_contact_inbox.loaded_at IS 'Момент последней записи версии строки в DDS.';

COMMENT ON COLUMN dds.bridge_contact_inbox.deleted_at IS 'Момент обнаружения исчезновения из выбранного среза (удаление или отзыв видимости). NULL у активной строки.';

CREATE TABLE IF NOT EXISTS dds.bridge_pool_aid (
    account_id bigint NOT NULL,
    pool_key uuid NOT NULL,
    aid_id text NOT NULL,
    row_hash text NOT NULL,
    loaded_at timestamptz NOT NULL,
    deleted_at timestamptz,
    PRIMARY KEY (account_id, pool_key, aid_id)
);

COMMENT ON TABLE dds.bridge_pool_aid IS 'Одна разрешённая пара вида помощи и пула, попавшего в область загрузки. Источник — women_help_certificate_pool_aids.';

COMMENT ON COLUMN dds.bridge_pool_aid.account_id IS 'Аккаунт Chatwoot, которому принадлежит строка DDS. NULL недопустим.';

COMMENT ON COLUMN dds.bridge_pool_aid.pool_key IS 'ID пула сертификатов. NULL недопустим.';

COMMENT ON COLUMN dds.bridge_pool_aid.aid_id IS 'Код вида помощи, для которого допустим этот пул. не фактическая выдача. NULL недопустим.';

COMMENT ON COLUMN dds.bridge_pool_aid.row_hash IS 'Хеш выбранных значений строки DDS для определения изменений.';

COMMENT ON COLUMN dds.bridge_pool_aid.loaded_at IS 'Момент последней записи версии строки в DDS.';

COMMENT ON COLUMN dds.bridge_pool_aid.deleted_at IS 'Момент обнаружения исчезновения из выбранного среза (удаление или отзыв видимости). NULL у активной строки.';

CREATE TABLE IF NOT EXISTS dds.bridge_certificate_request_event (
    account_id bigint NOT NULL,
    event_key uuid NOT NULL,
    certificate_request_key uuid NOT NULL,
    row_hash text NOT NULL,
    loaded_at timestamptz NOT NULL,
    deleted_at timestamptz,
    PRIMARY KEY (certificate_request_key, event_key)
);

COMMENT ON TABLE dds.bridge_certificate_request_event IS 'Одна уникальная ссылка на событие в записи очереди сертификатов. Источник — women_help_certificate_requests.source_event_keys.';

COMMENT ON COLUMN dds.bridge_certificate_request_event.account_id IS 'Аккаунт Chatwoot, которому принадлежит строка DDS. NULL недопустим.';

COMMENT ON COLUMN dds.bridge_certificate_request_event.event_key IS 'Псевдоним ссылки на событие из разрешённого формата исходного ключа. NULL недопустим.';

COMMENT ON COLUMN dds.bridge_certificate_request_event.certificate_request_key IS 'Ключ записи очереди сертификатов. NULL недопустим.';

COMMENT ON COLUMN dds.bridge_certificate_request_event.row_hash IS 'Хеш выбранных значений строки DDS для определения изменений.';

COMMENT ON COLUMN dds.bridge_certificate_request_event.loaded_at IS 'Момент последней записи версии строки в DDS.';

COMMENT ON COLUMN dds.bridge_certificate_request_event.deleted_at IS 'Момент обнаружения исчезновения из выбранного среза (удаление или отзыв видимости). NULL у активной строки.';

CREATE TABLE IF NOT EXISTS dds.bridge_label_tag (
    account_id bigint NOT NULL,
    label_key uuid NOT NULL,
    tag_key uuid NOT NULL,
    row_hash text NOT NULL,
    loaded_at timestamptz NOT NULL,
    deleted_at timestamptz,
    PRIMARY KEY (label_key, tag_key)
);

COMMENT ON TABLE dds.bridge_label_tag IS 'Одно допустимое соответствие метки и тега внутри аккаунта. Источник — labels.title и tags.name.';

COMMENT ON COLUMN dds.bridge_label_tag.account_id IS 'Аккаунт Chatwoot, которому принадлежит строка DDS. NULL недопустим.';

COMMENT ON COLUMN dds.bridge_label_tag.label_key IS 'Ключ метки из согласованного списка. NULL недопустим.';

COMMENT ON COLUMN dds.bridge_label_tag.tag_key IS 'Ключ тега с таким же разрешённым названием. NULL недопустим.';

COMMENT ON COLUMN dds.bridge_label_tag.row_hash IS 'Хеш выбранных значений строки DDS для определения изменений.';

COMMENT ON COLUMN dds.bridge_label_tag.loaded_at IS 'Момент последней записи версии строки в DDS.';

COMMENT ON COLUMN dds.bridge_label_tag.deleted_at IS 'Момент обнаружения исчезновения из выбранного среза (удаление или отзыв видимости). NULL у активной строки.';

CREATE TABLE IF NOT EXISTS dds.bridge_object_tag (
    account_id bigint NOT NULL,
    tagging_key uuid NOT NULL,
    tag_key uuid NOT NULL,
    taggable_type text NOT NULL,
    object_key uuid NOT NULL,
    context text NOT NULL,
    created_at timestamptz,
    row_hash text NOT NULL,
    loaded_at timestamptz NOT NULL,
    deleted_at timestamptz,
    PRIMARY KEY (tagging_key)
);

COMMENT ON TABLE dds.bridge_object_tag IS 'Одно каноническое назначение тега объекту в контексте. Источник — taggings.';

COMMENT ON COLUMN dds.bridge_object_tag.account_id IS 'Аккаунт Chatwoot, которому принадлежит строка DDS. NULL недопустим.';

COMMENT ON COLUMN dds.bridge_object_tag.tagging_key IS 'Псевдоним канонического назначения тега объекту. NULL недопустим.';

COMMENT ON COLUMN dds.bridge_object_tag.tag_key IS 'ID назначенного тега. NULL недопустим.';

COMMENT ON COLUMN dds.bridge_object_tag.taggable_type IS 'Тип объекта, которому назначен тег. NULL недопустим.';

COMMENT ON COLUMN dds.bridge_object_tag.object_key IS 'ID объекта, которому назначен тег. NULL недопустим.';

COMMENT ON COLUMN dds.bridge_object_tag.context IS 'Контекст назначения тега, необходимый для различения связей. NULL недопустим.';

COMMENT ON COLUMN dds.bridge_object_tag.created_at IS 'Самый ранний наблюдавшийся момент среди одинаковых исходных назначений. NULL означает отсутствие доступного корректного значения.';

COMMENT ON COLUMN dds.bridge_object_tag.row_hash IS 'Хеш выбранных значений строки DDS для определения изменений.';

COMMENT ON COLUMN dds.bridge_object_tag.loaded_at IS 'Момент последней записи версии строки в DDS.';

COMMENT ON COLUMN dds.bridge_object_tag.deleted_at IS 'Момент обнаружения исчезновения из выбранного среза (удаление или отзыв видимости). NULL у активной строки.';

CREATE TABLE IF NOT EXISTS dds.fact_conversation (
    account_id bigint NOT NULL,
    conversation_key uuid NOT NULL,
    contact_key uuid,
    inbox_key uuid,
    contact_inbox_key uuid,
    created_at timestamptz,
    status integer,
    status_changed_at timestamptz,
    assignee_staff_key uuid,
    updated_at timestamptz,
    assignee_bot_key uuid,
    ai_assignee_type text,
    row_hash text NOT NULL,
    loaded_at timestamptz NOT NULL,
    deleted_at timestamptz,
    PRIMARY KEY (conversation_key)
);

COMMENT ON TABLE dds.fact_conversation IS 'Один диалог Chatwoot. Источник — conversations.';

COMMENT ON COLUMN dds.fact_conversation.account_id IS 'Аккаунт Chatwoot, которому принадлежит строка DDS. NULL недопустим.';

COMMENT ON COLUMN dds.fact_conversation.conversation_key IS 'ID диалога. NULL недопустим.';

COMMENT ON COLUMN dds.fact_conversation.contact_key IS 'ID контакта, которому принадлежит диалог. NULL означает отсутствие доступного корректного значения.';

COMMENT ON COLUMN dds.fact_conversation.inbox_key IS 'ID канала диалога. NULL означает отсутствие доступного корректного значения.';

COMMENT ON COLUMN dds.fact_conversation.contact_inbox_key IS 'ID связи контакта с каналом. NULL означает отсутствие доступного корректного значения.';

COMMENT ON COLUMN dds.fact_conversation.created_at IS 'Дата создания диалога. NULL означает отсутствие доступного корректного значения.';

COMMENT ON COLUMN dds.fact_conversation.status IS 'Текущий код состояния диалога Chatwoot. Resolved означает закрытие диалога. NULL означает отсутствие доступного корректного значения.';

COMMENT ON COLUMN dds.fact_conversation.status_changed_at IS 'Дата последней смены состояния диалога. NULL означает отсутствие доступного корректного значения.';

COMMENT ON COLUMN dds.fact_conversation.assignee_staff_key IS 'ID назначенного специалиста/учётной записи. NULL означает отсутствие доступного корректного значения.';

COMMENT ON COLUMN dds.fact_conversation.updated_at IS 'Дата изменения диалога. NULL означает отсутствие доступного корректного значения.';

COMMENT ON COLUMN dds.fact_conversation.assignee_bot_key IS 'ID назначенного бота, если используется для отслеживания передачи человеку. NULL означает отсутствие доступного корректного значения.';

COMMENT ON COLUMN dds.fact_conversation.ai_assignee_type IS 'Тип назначения автоматизации, сохранённый в Chatwoot. NULL означает отсутствие доступного корректного значения.';

COMMENT ON COLUMN dds.fact_conversation.row_hash IS 'Хеш выбранных значений строки DDS для определения изменений.';

COMMENT ON COLUMN dds.fact_conversation.loaded_at IS 'Момент последней записи версии строки в DDS.';

COMMENT ON COLUMN dds.fact_conversation.deleted_at IS 'Момент обнаружения исчезновения из выбранного среза (удаление или отзыв видимости). NULL у активной строки.';

CREATE TABLE IF NOT EXISTS dds.fact_message (
    account_id bigint NOT NULL,
    message_key uuid NOT NULL,
    conversation_key uuid,
    inbox_key uuid,
    sender_key uuid,
    sender_type text,
    created_at timestamptz,
    updated_at timestamptz,
    message_type integer,
    private boolean,
    status integer,
    row_hash text NOT NULL,
    loaded_at timestamptz NOT NULL,
    deleted_at timestamptz,
    PRIMARY KEY (message_key)
);

COMMENT ON TABLE dds.fact_message IS 'Одно сообщение Chatwoot с выбранными метаданными. Источник — messages.';

COMMENT ON COLUMN dds.fact_message.account_id IS 'Аккаунт Chatwoot, которому принадлежит строка DDS. NULL недопустим.';

COMMENT ON COLUMN dds.fact_message.message_key IS 'ID сообщения. NULL недопустим.';

COMMENT ON COLUMN dds.fact_message.conversation_key IS 'ID диалога сообщения. NULL означает отсутствие доступного корректного значения.';

COMMENT ON COLUMN dds.fact_message.inbox_key IS 'ID канала сообщения. NULL означает отсутствие доступного корректного значения.';

COMMENT ON COLUMN dds.fact_message.sender_key IS 'Псевдоним отправителя. Соединять по ключу, аккаунту и условию на sender_type. NULL означает отсутствие доступного корректного значения.';

COMMENT ON COLUMN dds.fact_message.sender_type IS 'Тип сущности отправителя. Значение User само по себе не подтверждает ручное авторство. NULL означает отсутствие доступного корректного значения.';

COMMENT ON COLUMN dds.fact_message.created_at IS 'Дата создания сообщения. NULL означает отсутствие доступного корректного значения.';

COMMENT ON COLUMN dds.fact_message.updated_at IS 'Дата изменения метаданных сообщения. NULL означает отсутствие доступного корректного значения.';

COMMENT ON COLUMN dds.fact_message.message_type IS 'Код направления или служебного назначения сообщения Chatwoot. NULL означает отсутствие доступного корректного значения.';

COMMENT ON COLUMN dds.fact_message.private IS 'Признак внутреннего сообщения. NULL означает отсутствие доступного корректного значения.';

COMMENT ON COLUMN dds.fact_message.status IS 'Код состояния доставки сообщения Chatwoot. NULL означает отсутствие доступного корректного значения.';

COMMENT ON COLUMN dds.fact_message.row_hash IS 'Хеш выбранных значений строки DDS для определения изменений.';

COMMENT ON COLUMN dds.fact_message.loaded_at IS 'Момент последней записи версии строки в DDS.';

COMMENT ON COLUMN dds.fact_message.deleted_at IS 'Момент обнаружения исчезновения из выбранного среза (удаление или отзыв видимости). NULL у активной строки.';

CREATE TABLE IF NOT EXISTS dds.fact_consultation_request (
    account_id bigint NOT NULL,
    request_key uuid NOT NULL,
    state text,
    aid_id text,
    requested_at timestamptz,
    completed_at timestamptz,
    specialist_staff_key uuid,
    completed_message_key uuid,
    conversation_key uuid NOT NULL,
    row_hash text NOT NULL,
    loaded_at timestamptz NOT NULL,
    deleted_at timestamptz,
    PRIMARY KEY (request_key)
);

COMMENT ON TABLE dds.fact_consultation_request IS 'Одна запись запроса консультации внутри диалога. Источник — conversations.custom_attributes.scenario_requests.';

COMMENT ON COLUMN dds.fact_consultation_request.account_id IS 'Аккаунт Chatwoot, которому принадлежит строка DDS. NULL недопустим.';

COMMENT ON COLUMN dds.fact_consultation_request.request_key IS 'Псевдоним отдельной записи запроса внутри диалога. NULL недопустим.';

COMMENT ON COLUMN dds.fact_consultation_request.state IS 'Состояние запроса консультации. Для записи с отсутствующим или недопустимым состоянием выводится запись контроля качества.';

COMMENT ON COLUMN dds.fact_consultation_request.aid_id IS 'Код запрошенного вида помощи. NULL означает отсутствие доступного корректного значения.';

COMMENT ON COLUMN dds.fact_consultation_request.requested_at IS 'Дата запроса консультации, если сохранена. NULL означает отсутствие доступного корректного значения.';

COMMENT ON COLUMN dds.fact_consultation_request.completed_at IS 'Момент отметки завершения запроса. Факт состоявшейся консультации неизвестен. NULL означает отсутствие доступного корректного значения.';

COMMENT ON COLUMN dds.fact_consultation_request.specialist_staff_key IS 'ID специалиста, связанного с запросом. NULL означает отсутствие доступного корректного значения.';

COMMENT ON COLUMN dds.fact_consultation_request.completed_message_key IS 'ID сообщения, связанного с отметкой завершения запроса. NULL означает отсутствие доступного корректного значения.';

COMMENT ON COLUMN dds.fact_consultation_request.conversation_key IS 'Ключ родительского диалога исходной записи запроса. NULL недопустим.';

COMMENT ON COLUMN dds.fact_consultation_request.row_hash IS 'Хеш выбранных значений строки DDS для определения изменений.';

COMMENT ON COLUMN dds.fact_consultation_request.loaded_at IS 'Момент последней записи версии строки в DDS.';

COMMENT ON COLUMN dds.fact_consultation_request.deleted_at IS 'Момент обнаружения исчезновения из выбранного среза (удаление или отзыв видимости). NULL у активной строки.';

CREATE TABLE IF NOT EXISTS dds.fact_followup (
    account_id bigint NOT NULL,
    followup_key uuid NOT NULL,
    state text,
    due_at timestamptz,
    sent_at timestamptz,
    aid_id text,
    request_key uuid,
    screen_code text,
    conversation_key uuid NOT NULL,
    target_kind text,
    unresolved_target_key uuid,
    row_hash text NOT NULL,
    loaded_at timestamptz NOT NULL,
    deleted_at timestamptz,
    PRIMARY KEY (followup_key)
);

COMMENT ON TABLE dds.fact_followup IS 'Одна запись follow-up внутри диалога. Источник — conversations.custom_attributes.scenario_followups.';

COMMENT ON COLUMN dds.fact_followup.account_id IS 'Аккаунт Chatwoot, которому принадлежит строка DDS. NULL недопустим.';

COMMENT ON COLUMN dds.fact_followup.followup_key IS 'Псевдоним отдельной записи follow-up внутри диалога. NULL недопустим.';

COMMENT ON COLUMN dds.fact_followup.state IS 'Состояние follow-up. Для записи с отсутствующим или недопустимым состоянием выводится запись контроля качества.';

COMMENT ON COLUMN dds.fact_followup.due_at IS 'Плановая дата отправки follow-up. NULL означает отсутствие доступного корректного значения.';

COMMENT ON COLUMN dds.fact_followup.sent_at IS 'Момент отметки отправки follow-up. Получение ответа отражается отдельно в состоянии. NULL означает отсутствие доступного корректного значения.';

COMMENT ON COLUMN dds.fact_followup.aid_id IS 'Код вида помощи, к которому относится follow-up. NULL означает отсутствие доступного корректного значения.';

COMMENT ON COLUMN dds.fact_followup.request_key IS 'Ключ консультационного запроса, разрешённый в том же диалоге. NULL при отсутствии ссылки этого типа.';

COMMENT ON COLUMN dds.fact_followup.screen_code IS 'Разрешённый код экрана, связанного с follow-up. NULL означает отсутствие доступного корректного значения.';

COMMENT ON COLUMN dds.fact_followup.conversation_key IS 'Ключ родительского диалога исходной записи follow-up. NULL недопустим.';

COMMENT ON COLUMN dds.fact_followup.target_kind IS 'Категория связи follow-up с исходным запросом. NULL означает отсутствие доступного корректного значения.';

COMMENT ON COLUMN dds.fact_followup.unresolved_target_key IS 'Непрозрачная ссылка follow-up, которую не удалось связать с консультацией. NULL при отсутствии ссылки этого типа.';

COMMENT ON COLUMN dds.fact_followup.row_hash IS 'Хеш выбранных значений строки DDS для определения изменений.';

COMMENT ON COLUMN dds.fact_followup.loaded_at IS 'Момент последней записи версии строки в DDS.';

COMMENT ON COLUMN dds.fact_followup.deleted_at IS 'Момент обнаружения исчезновения из выбранного среза (удаление или отзыв видимости). NULL у активной строки.';

CREATE TABLE IF NOT EXISTS dds.fact_certificate (
    account_id bigint NOT NULL,
    certificate_key uuid NOT NULL,
    pool_key uuid,
    contact_key uuid,
    conversation_key uuid,
    source_message_key uuid,
    send_message_key uuid,
    nominal_rubles integer,
    selected_aid_id text,
    status text,
    record_kind text,
    is_test boolean,
    valid_from timestamptz,
    expires_at timestamptz,
    reserved_at timestamptz,
    submitted_at timestamptz,
    delivered_at timestamptz,
    accepted_at timestamptz,
    issued_on date,
    resolved_contact_key uuid,
    resolved_conversation_key uuid,
    link_origin text,
    row_hash text NOT NULL,
    loaded_at timestamptz NOT NULL,
    deleted_at timestamptz,
    PRIMARY KEY (certificate_key)
);

COMMENT ON TABLE dds.fact_certificate IS 'Одна запись сертификата, связанная с областью загрузки через исторический диалог или сообщение отправки. Источник — women_help_pdf_certificates.';

COMMENT ON COLUMN dds.fact_certificate.account_id IS 'Аккаунт Chatwoot, которому принадлежит строка DDS. NULL недопустим.';

COMMENT ON COLUMN dds.fact_certificate.certificate_key IS 'ID исходной записи сертификата. NULL недопустим.';

COMMENT ON COLUMN dds.fact_certificate.pool_key IS 'ID пула сертификата. NULL означает отсутствие доступного корректного значения.';

COMMENT ON COLUMN dds.fact_certificate.contact_key IS 'ID связанного контакта, если назначен. NULL сохраняет отсутствие прямой связи в источнике.';

COMMENT ON COLUMN dds.fact_certificate.conversation_key IS 'ID связанного диалога. NULL сохраняет отсутствие прямой связи в источнике.';

COMMENT ON COLUMN dds.fact_certificate.source_message_key IS 'ID исходного сообщения, связанного с записью сертификата. NULL означает отсутствие доступного корректного значения.';

COMMENT ON COLUMN dds.fact_certificate.send_message_key IS 'Ключ сообщения Chatwoot, связанного с отправкой сертификата. NULL означает отсутствие доступного корректного значения.';

COMMENT ON COLUMN dds.fact_certificate.nominal_rubles IS 'Номинал сертификата в рублях. NULL означает отсутствие доступного корректного значения.';

COMMENT ON COLUMN dds.fact_certificate.selected_aid_id IS 'Код выбранного вида помощи. NULL означает отсутствие доступного корректного значения.';

COMMENT ON COLUMN dds.fact_certificate.status IS 'Состояние записи сертификата, связанной с областью загрузки. Сопоставлять состояние с record_kind. Доступный остаток неизвестен. NULL означает отсутствие доступного корректного значения.';

COMMENT ON COLUMN dds.fact_certificate.record_kind IS 'Вид записи. Certificate обозначает документ инвентаря, historical_issuance — историческую запись выдачи. NULL означает отсутствие доступного корректного значения.';

COMMENT ON COLUMN dds.fact_certificate.is_test IS 'Признак тестового сертификата. NULL означает отсутствие доступного корректного значения.';

COMMENT ON COLUMN dds.fact_certificate.valid_from IS 'Начало срока действия сертификата. NULL означает отсутствие доступного корректного значения.';

COMMENT ON COLUMN dds.fact_certificate.expires_at IS 'Окончание срока действия сертификата. NULL означает отсутствие доступного корректного значения.';

COMMENT ON COLUMN dds.fact_certificate.reserved_at IS 'Дата отметки резервирования сертификата. NULL означает отсутствие доступного корректного значения.';

COMMENT ON COLUMN dds.fact_certificate.submitted_at IS 'Момент отметки этапа submitted в источнике. NULL означает отсутствие доступного корректного значения.';

COMMENT ON COLUMN dds.fact_certificate.delivered_at IS 'Момент отметки доставки. Факт использования сертификата неизвестен. NULL означает отсутствие доступного корректного значения.';

COMMENT ON COLUMN dds.fact_certificate.accepted_at IS 'Момент отметки принятия в источнике, включая импорт исторических записей. NULL означает отсутствие доступного корректного значения.';

COMMENT ON COLUMN dds.fact_certificate.issued_on IS 'Календарная дата выдачи, указанная источником. NULL означает отсутствие доступного корректного значения.';

COMMENT ON COLUMN dds.fact_certificate.resolved_contact_key IS 'Контакт из прямой ссылки или из диалога отправки сертификата. NULL, если безопасная связь не установлена.';

COMMENT ON COLUMN dds.fact_certificate.resolved_conversation_key IS 'Диалог из прямой ссылки или из сообщения отправки сертификата. NULL, если безопасная связь не установлена.';

COMMENT ON COLUMN dds.fact_certificate.link_origin IS 'Происхождение связей сертификата с диалогом или контактом. NULL означает отсутствие доступного корректного значения.';

COMMENT ON COLUMN dds.fact_certificate.row_hash IS 'Хеш выбранных значений строки DDS для определения изменений.';

COMMENT ON COLUMN dds.fact_certificate.loaded_at IS 'Момент последней записи версии строки в DDS.';

COMMENT ON COLUMN dds.fact_certificate.deleted_at IS 'Момент обнаружения исчезновения из выбранного среза (удаление или отзыв видимости). NULL у активной строки.';

CREATE TABLE IF NOT EXISTS dds.fact_certificate_request (
    account_id bigint NOT NULL,
    certificate_request_key uuid NOT NULL,
    contact_key uuid,
    conversation_key uuid,
    source_message_key uuid,
    last_conversation_key uuid,
    last_source_message_key uuid,
    aid_id text,
    pool_key uuid,
    reason text,
    status text,
    first_requested_at timestamptz,
    last_requested_at timestamptz,
    fulfilled_certificate_key uuid,
    row_hash text NOT NULL,
    loaded_at timestamptz NOT NULL,
    deleted_at timestamptz,
    PRIMARY KEY (certificate_request_key)
);

COMMENT ON TABLE dds.fact_certificate_request IS 'Одна запись в очереди запросов сертификата. Источник — women_help_certificate_requests.';

COMMENT ON COLUMN dds.fact_certificate_request.account_id IS 'Аккаунт Chatwoot, которому принадлежит строка DDS. NULL недопустим.';

COMMENT ON COLUMN dds.fact_certificate_request.certificate_request_key IS 'ID записи запроса в очереди сертификатов. NULL недопустим.';

COMMENT ON COLUMN dds.fact_certificate_request.contact_key IS 'ID контакта, запросившего сертификат. NULL означает отсутствие доступного корректного значения.';

COMMENT ON COLUMN dds.fact_certificate_request.conversation_key IS 'ID исходного диалога запроса. NULL означает отсутствие доступного корректного значения.';

COMMENT ON COLUMN dds.fact_certificate_request.source_message_key IS 'ID исходного сообщения запроса. NULL означает отсутствие доступного корректного значения.';

COMMENT ON COLUMN dds.fact_certificate_request.last_conversation_key IS 'ID последнего связанного диалога. NULL означает отсутствие доступного корректного значения.';

COMMENT ON COLUMN dds.fact_certificate_request.last_source_message_key IS 'ID последнего связанного сообщения. NULL означает отсутствие доступного корректного значения.';

COMMENT ON COLUMN dds.fact_certificate_request.aid_id IS 'Код запрошенного вида помощи. NULL означает отсутствие доступного корректного значения.';

COMMENT ON COLUMN dds.fact_certificate_request.pool_key IS 'ID связанного пула сертификатов. NULL означает отсутствие доступного корректного значения.';

COMMENT ON COLUMN dds.fact_certificate_request.reason IS 'Разрешённый код причины попадания запроса в очередь. NULL означает отсутствие доступного корректного значения.';

COMMENT ON COLUMN dds.fact_certificate_request.status IS 'Состояние запроса сертификата. NULL означает отсутствие доступного корректного значения.';

COMMENT ON COLUMN dds.fact_certificate_request.first_requested_at IS 'Первая дата запроса, сохранённая в записи. NULL означает отсутствие доступного корректного значения.';

COMMENT ON COLUMN dds.fact_certificate_request.last_requested_at IS 'Последняя сохранённая дата запроса в записи очереди. Число попыток неизвестно. NULL означает отсутствие доступного корректного значения.';

COMMENT ON COLUMN dds.fact_certificate_request.fulfilled_certificate_key IS 'ID сертификата, связанного с исполнением запроса. NULL означает отсутствие доступного корректного значения.';

COMMENT ON COLUMN dds.fact_certificate_request.row_hash IS 'Хеш выбранных значений строки DDS для определения изменений.';

COMMENT ON COLUMN dds.fact_certificate_request.loaded_at IS 'Момент последней записи версии строки в DDS.';

COMMENT ON COLUMN dds.fact_certificate_request.deleted_at IS 'Момент обнаружения исчезновения из выбранного среза (удаление или отзыв видимости). NULL у активной строки.';

CREATE TABLE IF NOT EXISTS dds.fact_csat (
    account_id bigint NOT NULL,
    csat_key uuid NOT NULL,
    conversation_key uuid,
    message_key uuid,
    contact_key uuid,
    rating integer,
    created_at timestamptz,
    updated_at timestamptz,
    row_hash text NOT NULL,
    loaded_at timestamptz NOT NULL,
    deleted_at timestamptz,
    PRIMARY KEY (csat_key)
);

COMMENT ON TABLE dds.fact_csat IS 'Одна запись ответа на опрос CSAT. Источник — csat_survey_responses.';

COMMENT ON COLUMN dds.fact_csat.account_id IS 'Аккаунт Chatwoot, которому принадлежит строка DDS. NULL недопустим.';

COMMENT ON COLUMN dds.fact_csat.csat_key IS 'ID ответа на опрос CSAT. NULL недопустим.';

COMMENT ON COLUMN dds.fact_csat.conversation_key IS 'ID оцениваемого диалога. NULL означает отсутствие доступного корректного значения.';

COMMENT ON COLUMN dds.fact_csat.message_key IS 'Ключ сообщения, связанного с ответом на опрос CSAT. NULL означает отсутствие доступного корректного значения.';

COMMENT ON COLUMN dds.fact_csat.contact_key IS 'ID контакта, с которым связан ответ. NULL означает отсутствие доступного корректного значения.';

COMMENT ON COLUMN dds.fact_csat.rating IS 'Оценка ответа CSAT по шкале от 1 до 5. NULL означает отсутствие доступного корректного значения.';

COMMENT ON COLUMN dds.fact_csat.created_at IS 'Дата создания ответа CSAT. NULL означает отсутствие доступного корректного значения.';

COMMENT ON COLUMN dds.fact_csat.updated_at IS 'Дата изменения ответа CSAT. NULL означает отсутствие доступного корректного значения.';

COMMENT ON COLUMN dds.fact_csat.row_hash IS 'Хеш выбранных значений строки DDS для определения изменений.';

COMMENT ON COLUMN dds.fact_csat.loaded_at IS 'Момент последней записи версии строки в DDS.';

COMMENT ON COLUMN dds.fact_csat.deleted_at IS 'Момент обнаружения исчезновения из выбранного среза (удаление или отзыв видимости). NULL у активной строки.';
