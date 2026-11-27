-- Объёмы создания и завершения по датам соответствующих отметок.
SELECT
    account_id,
    (requested_at AT TIME ZONE 'Europe/Moscow')::date AS day_moscow,
    count(*) AS consultation_requests_created
FROM dds.fact_consultation_request
WHERE deleted_at IS NULL
  AND requested_at IS NOT NULL
GROUP BY account_id, day_moscow
ORDER BY account_id, day_moscow;

SELECT
    account_id,
    (completed_at AT TIME ZONE 'Europe/Moscow')::date AS day_moscow,
    count(*) AS consultation_requests_completed
FROM dds.fact_consultation_request
WHERE deleted_at IS NULL
  AND state = 'completed'
  AND completed_at IS NOT NULL
GROUP BY account_id, day_moscow
ORDER BY account_id, day_moscow;

-- Наблюдавшиеся записи сертификатов по record_kind и состоянию. Остаток склада неизвестен.
SELECT account_id, record_kind, status, count(*) AS certificate_records
FROM dds.fact_certificate
WHERE deleted_at IS NULL
  AND is_test IS FALSE
GROUP BY account_id, record_kind, status
ORDER BY account_id, record_kind, status;

-- Текущее состояние очереди. Каждая запись считается один раз.
SELECT account_id, status, count(*) AS certificate_request_queue_rows
FROM dds.fact_certificate_request
WHERE deleted_at IS NULL
GROUP BY account_id, status
ORDER BY account_id, status;

-- Средняя оценка и размер выборки.
SELECT
    account_id,
    (created_at AT TIME ZONE 'Europe/Moscow')::date AS day_moscow,
    avg(rating) AS csat_mean_rating,
    count(*) AS csat_rated_responses
FROM dds.fact_csat
WHERE deleted_at IS NULL
  AND created_at IS NOT NULL
  AND rating BETWEEN 1 AND 5
GROUP BY account_id, day_moscow
ORDER BY account_id, day_moscow;

-- Сообщения и запросы приведены к гранулярности диалога до соединения.
WITH message_counts AS (
    SELECT account_id, conversation_key, count(*) AS messages
    FROM dds.fact_message
    WHERE deleted_at IS NULL AND private IS FALSE AND message_type IN (0, 1)
    GROUP BY account_id, conversation_key
), request_counts AS (
    SELECT account_id, conversation_key, count(*) AS consultation_requests
    FROM dds.fact_consultation_request
    WHERE deleted_at IS NULL
    GROUP BY account_id, conversation_key
)
SELECT
    conversation.account_id,
    conversation.conversation_key,
    coalesce(message_counts.messages, 0) AS messages,
    coalesce(request_counts.consultation_requests, 0) AS consultation_requests
FROM dds.fact_conversation AS conversation
LEFT JOIN message_counts USING (account_id, conversation_key)
LEFT JOIN request_counts USING (account_id, conversation_key)
WHERE conversation.deleted_at IS NULL
ORDER BY conversation.account_id, conversation.conversation_key;

-- Проверка наличия согласованной метки через EXISTS сохраняет гранулярность.
SELECT conversation.account_id, count(*) AS conversations_with_approved_label
FROM dds.fact_conversation AS conversation
WHERE conversation.deleted_at IS NULL
  AND EXISTS (
      SELECT 1
      FROM dds.bridge_object_tag AS tagging
      JOIN dds.bridge_label_tag AS label_tag
        ON label_tag.account_id = tagging.account_id
       AND label_tag.tag_key = tagging.tag_key
       AND label_tag.deleted_at IS NULL
      JOIN dds.dim_label AS label
        ON label.account_id = label_tag.account_id
       AND label.label_key = label_tag.label_key
       AND label.deleted_at IS NULL
      WHERE tagging.account_id = conversation.account_id
        AND tagging.object_key = conversation.conversation_key
        AND tagging.taggable_type = 'Conversation'
        AND tagging.context = 'labels'
        AND tagging.deleted_at IS NULL
  )
GROUP BY conversation.account_id
ORDER BY conversation.account_id;

-- Факт оказания помощи не измеряется этим DDS.
SELECT NULL::bigint AS actual_help, 'неизвестно'::text AS availability;


-- Полнота дат — знаменатель включает неизвестные даты, без назначения их произвольному дню.
SELECT account_id, count(*) AS all_active_scoped_requests,
    count(requested_at) AS known_requested_at,
    count(*) FILTER (WHERE requested_at IS NULL) AS missing_requested_at,
    count(*) FILTER (WHERE state='completed' AND completed_at IS NULL) AS completed_missing_date
FROM dds.fact_consultation_request WHERE deleted_at IS NULL GROUP BY account_id;

SELECT account_id, count(*) AS active_sent_or_answered_followups,
    count(sent_at) AS known_sent_at,
    count(*) FILTER (WHERE sent_at IS NULL) AS missing_sent_at
FROM dds.fact_followup WHERE deleted_at IS NULL AND state IN ('sent','answered')
GROUP BY account_id;
