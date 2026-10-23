-- Read only: execute inside BEGIN READ ONLY with a statement timeout.
-- This snapshot is scoped to the verified production Telegram inbox, not API/MVP tests.
-- Output contains pseudonymous contact keys and event timestamps, never dialogue text.
WITH scope AS (
    SELECT i.id FROM inboxes i JOIN channel_telegram t ON t.id = i.channel_id
    WHERE i.account_id = 1 AND i.id = 5 AND i.channel_type = 'Channel::Telegram'
      AND t.bot_name = 'nevidima_bot'
), scoped_conversations AS (
    SELECT c.* FROM conversations c JOIN scope s ON s.id = c.inbox_id
    WHERE c.account_id = 1
), m AS (
    SELECT m.*, c.contact_id,
      CASE jsonb_typeof(m.content_attributes::jsonb)
        WHEN 'object' THEN m.content_attributes::jsonb
        WHEN 'string' THEN (m.content_attributes::jsonb #>> '{}')::jsonb
        ELSE '{}'::jsonb END AS attrs
    FROM messages m JOIN scoped_conversations c ON c.id = m.conversation_id
    WHERE m.account_id = 1 AND m.inbox_id = c.inbox_id
), events AS (
    SELECT *, attrs ->> 'nevidimy_telegram_callback_data' AS callback,
      NOT private AND message_type = 0 AS incoming,
      NOT private AND message_type = 1 AS outgoing,
      coalesce(additional_attributes ? 'legacy_telegram_import', false) AS imported
    FROM m
), certificate_messages AS (
    -- Same historical-evidence criteria as scripts/backfill_certificate_history.py.
    -- Telegram source_id is a send receipt, not evidence of reading or redemption.
    SELECT DISTINCT m.id
    FROM m JOIN attachments a ON a.message_id = m.id
    JOIN active_storage_attachments aa ON aa.record_type = 'Attachment'
      AND aa.record_id = a.id AND aa.name = 'file'
    JOIN active_storage_blobs b ON b.id = aa.blob_id
    WHERE m.message_type = 1 AND NOT m.private AND m.status <> 3
      AND coalesce(m.source_id, '') <> ''
      AND b.content_type = 'application/pdf'
      AND b.filename ~* '(certificate|certificat|ozon|pyater|сертифик|озон|пят[её]р)'
      AND m.content ILIKE '%сертификат%'
), completion AS (
    SELECT c.contact_id, r.v ->> 'completed_at' AS at
    FROM scoped_conversations c
    CROSS JOIN LATERAL jsonb_each(coalesce(c.custom_attributes -> 'scenario_requests', '{}')) r(k,v)
    WHERE r.v ->> 'state' = 'completed'
      AND r.v ->> 'completed_at' IS NOT NULL
      AND r.v ->> 'completed_message_id' IS NOT NULL
), known_test_contacts AS (
    -- Owner's nonce-verified production test conversation; never count it as a client.
    SELECT contact_id FROM scoped_conversations WHERE display_id = 62
    UNION
    SELECT contact_id FROM events WHERE source_id ~ '^nevidimy_perf_[0-9a-f]{12}$'
      OR (incoming AND content ~ '^/start nevidimy_e2e_')
), features AS (
    SELECT contact_id, count(*) AS message_count, bool_or(imported) AS imported,
      array_agg(created_at AT TIME ZONE 'UTC' ORDER BY created_at)
        FILTER (WHERE incoming AND content ~* '^/start(@[a-zA-Z0-9_]+)?([[:space:]]|$)') AS starts,
      array_agg(created_at AT TIME ZONE 'UTC' ORDER BY created_at)
        FILTER (WHERE incoming AND (callback = 'continue' OR btrim(content) = 'Да, посмотреть')) AS continues,
      array_agg(created_at AT TIME ZONE 'UTC' ORDER BY created_at)
        FILTER (WHERE outgoing AND (content ILIKE '%Что сейчас важнее всего%'
          OR EXISTS (SELECT 1 FROM jsonb_array_elements(
            CASE WHEN jsonb_typeof(attrs -> 'items') = 'array' THEN attrs -> 'items' ELSE '[]' END
          ) item WHERE item ->> 'value' LIKE 'need:%'))) AS menu_shown,
      array_agg(created_at AT TIME ZONE 'UTC' ORDER BY created_at)
        FILTER (WHERE incoming AND callback ~ '^need:(housing|food_money|food_hygiene|legal|support|children|other)$') AS menu_answers,
      array_agg(created_at AT TIME ZONE 'UTC' ORDER BY created_at)
        FILTER (WHERE incoming AND callback ~ '^aid:(food_card|medicine_card|hostel_3_nights|children_card|psychologist_3_sessions|legal_consultation|peer_consultation)$') AS aid_selected,
      array_agg(created_at AT TIME ZONE 'UTC' ORDER BY created_at)
        FILTER (WHERE id IN (SELECT id FROM certificate_messages)) AS certificate_delivered
    FROM events GROUP BY contact_id
)
SELECT json_build_object(
    'contact_key', md5('help-funnel-contact:' || c.contact_id::text),
    'imported', coalesce(f.imported, false),
    'excluded', c.contact_id IN (SELECT contact_id FROM known_test_contacts),
    'starts', coalesce(f.starts, ARRAY[]::timestamptz[]),
    'continues', coalesce(f.continues, ARRAY[]::timestamptz[]),
    'menu_shown', coalesce(f.menu_shown, ARRAY[]::timestamptz[]),
    'menu_answers', coalesce(f.menu_answers, ARRAY[]::timestamptz[]),
    'aid_selected', coalesce(f.aid_selected, ARRAY[]::timestamptz[]),
    'certificate_delivered', coalesce(f.certificate_delivered, ARRAY[]::timestamptz[]),
    'consultation_completed', coalesce((SELECT json_agg(at) FROM completion d WHERE d.contact_id = c.contact_id), '[]'),
    'message_count', coalesce(f.message_count, 0)
)
FROM (SELECT DISTINCT contact_id FROM scoped_conversations) c
LEFT JOIN features f ON f.contact_id = c.contact_id
ORDER BY c.contact_id;
