-- Read-only snapshot. All aggregation stays in PostgreSQL.
-- Bind account ID, inbox ID, bot name and known test conversation display IDs, in that order.
WITH scope AS (
 SELECT i.id FROM inboxes i JOIN channel_telegram t ON t.id=i.channel_id
 WHERE i.account_id=$1::bigint AND i.id=$2::bigint AND i.channel_type='Channel::Telegram' AND t.bot_name=$3::text
), c AS (
 SELECT c.* FROM conversations c JOIN scope s ON s.id=c.inbox_id WHERE c.account_id=$1 AND c.contact_id IS NOT NULL
), m0 AS (
 SELECT m.*,c.contact_id,
 CASE jsonb_typeof(m.content_attributes::jsonb)
 WHEN 'object' THEN m.content_attributes::jsonb
 WHEN 'string' THEN (m.content_attributes::jsonb #>> '{}')::jsonb
 ELSE '{}'::jsonb END attrs
 FROM messages m JOIN c ON c.id=m.conversation_id WHERE m.account_id=$1 AND m.inbox_id=$2
), m AS (
 SELECT *,NOT private AND message_type=0 incoming,NOT private AND message_type=1 outgoing,
 coalesce(attrs->>'nevidimy_telegram_callback_data',CASE WHEN content ~ '^(need:|aid:|extra:|support:|followup:|sc:)[a-z0-9_:]+$' THEN content END) cb,
 substring(content FROM '^Нажата кнопка: «([^»]{1,90})»\.$') button_label
 FROM m0
), test_contacts AS (
 SELECT contact_id FROM c WHERE display_id=ANY($4::bigint[])
 UNION SELECT contact_id FROM m WHERE source_id ~ '^nevidimy_perf_[0-9a-f]{12}$' OR (incoming AND content ~ '^/start nevidimy_e2e_')
), clients AS (
 SELECT c.contact_id,coalesce(bool_or(m.additional_attributes ? 'legacy_telegram_import'),false) imported
 FROM c LEFT JOIN m ON m.conversation_id=c.id
 WHERE c.contact_id NOT IN(SELECT contact_id FROM test_contacts)
 GROUP BY c.contact_id
), menu AS (
 SELECT * FROM m WHERE outgoing AND (content ILIKE '%Что сейчас важнее всего%' OR EXISTS(
 SELECT 1 FROM jsonb_array_elements(CASE WHEN jsonb_typeof(attrs->'items')='array' THEN attrs->'items' ELSE '[]' END) x WHERE x->>'value' LIKE 'need:%'))
), incoming AS (
 SELECT *,regexp_replace(cb,'^(sc:[a-z0-9_]+:[0-9]+):.*$','\1') code FROM m WHERE incoming
), button_events AS (
 SELECT contact_id,'started' metric,'total' category FROM incoming WHERE content ~* '^/start(@[a-zA-Z0-9_]+)?([[:space:]]|$)'
 UNION ALL
 SELECT contact_id,'menu_answered','total' FROM incoming x WHERE cb IS NOT NULL AND
 (cb ~ '^need:(housing|food_money|food_hygiene|legal|support|children|other)$' OR EXISTS(
 SELECT 1 FROM menu p WHERE p.conversation_id=x.conversation_id AND
 (x.attrs->>'in_reply_to'=p.id::text OR x.attrs->>'in_reply_to_external_id'=p.source_id)))
 UNION ALL
 SELECT contact_id,'certificate_selected',CASE cb WHEN 'aid:food_card' THEN 'food' WHEN 'aid:children_card' THEN 'children' WHEN 'aid:medicine_card' THEN 'hygiene' WHEN 'aid:hostel_3_nights' THEN 'hostel' END
 FROM incoming WHERE cb IN('aid:food_card','aid:children_card','aid:medicine_card','aid:hostel_3_nights')
 UNION ALL
 SELECT contact_id,'service_selected',CASE
 WHEN cb IN('aid:legal_consultation','extra:legal') OR code IN('sc:s35:0','sc:s31b:1','sc:s32b:1','sc:s33c:1','sc:s54:1','sc:s37b:0') THEN 'legal'
 WHEN cb IN('aid:psychologist_3_sessions','extra:psychologist','support:psychologist') OR code IN('sc:s34:0','sc:s31b:0','sc:s32b:0','sc:s33c:0','sc:s54:0','sc:s37b:1') THEN 'psychologist'
 WHEN cb='aid:peer_consultation' OR code='sc:s38:0' THEN 'peer'
 WHEN code ~ '^sc:(i8|i9):' AND button_label IN('Консультация психолога','Поговорить с психологом') THEN 'psychologist'
 WHEN code ~ '^sc:(i8|i9):' AND button_label IN('Консультация юриста','Задать вопрос юристу') THEN 'legal'
 WHEN code ~ '^sc:(i8|i9):' AND button_label IN('Разговор с консультанткой по теме зависимости/созависимости','Разговор с равной консультанткой') THEN 'peer'
 END FROM incoming
 UNION ALL
 SELECT contact_id,'followup_replies',CASE
 WHEN cb='followup:better' OR code IN('sc:s52:0','sc:s5:28') THEN 'better'
 WHEN cb='followup:same' OR code IN('sc:s52:1','sc:s5:29') THEN 'same'
 WHEN cb='followup:worse' OR code IN('sc:s52:2','sc:s5:30') THEN 'worse'
 WHEN code IN('sc:s5:1','sc:s5:5','sc:s5:9','sc:s5:13','sc:s5:2','sc:s5:6','sc:s5:10','sc:s5:14','sc:s59:1','sc:s59:2','sc:s5:17','sc:s5:21','sc:s5:25','sc:s59:5') THEN 'not_used'
 END FROM incoming
), cert_pdf AS (
 SELECT DISTINCT m.id,m.contact_id FROM m JOIN attachments a ON a.message_id=m.id
 JOIN active_storage_attachments aa ON aa.record_type='Attachment' AND aa.record_id=a.id AND aa.name='file'
 JOIN active_storage_blobs b ON b.id=aa.blob_id
 WHERE m.outgoing AND m.status<>3 AND coalesce(m.source_id,'')<>''
 AND b.content_type='application/pdf' AND b.filename ~* '(certificate|certificat|ozon|pyater|сертифик|озон|пят[её]р)' AND m.content ILIKE '%сертификат%'
), registry AS (
 SELECT DISTINCT m.id,m.contact_id,CASE p.selected_aid_id WHEN 'food_card' THEN 'food' WHEN 'children_card' THEN 'children' WHEN 'medicine_card' THEN 'hygiene' WHEN 'hostel_3_nights' THEN 'hostel' ELSE 'unknown' END category
 FROM women_help_pdf_certificates p JOIN m ON m.id=coalesce(p.source_message_id,p.chatwoot_message_id)
 WHERE NOT p.is_test AND ((p.record_kind='historical_issuance' AND p.status='accepted') OR (p.record_kind='certificate' AND p.status='delivered'))
 AND m.outgoing AND m.status<>3 AND coalesce(m.source_id,'')<>''
), cert_events AS (
 SELECT contact_id,'certificate_sent' metric,category FROM registry
 UNION ALL SELECT p.contact_id,'certificate_sent','unknown' FROM cert_pdf p
 WHERE NOT EXISTS(SELECT 1 FROM registry r WHERE r.id=p.id)
 AND NOT EXISTS(SELECT 1 FROM women_help_pdf_certificates r WHERE r.is_test AND (r.source_message_id=p.id OR r.chatwoot_message_id=p.id))
), request_events AS (
 SELECT c.contact_id,'service_completed' metric,CASE r.value->>'aid_id' WHEN 'legal_consultation' THEN 'legal' WHEN 'psychologist_3_sessions' THEN 'psychologist' WHEN 'peer_consultation' THEN 'peer' END category
 FROM c CROSS JOIN LATERAL jsonb_each(coalesce(c.custom_attributes->'scenario_requests','{}')) r
 WHERE r.value->>'state'='completed' AND r.value->>'completed_at' IS NOT NULL AND r.value->>'completed_message_id' IS NOT NULL
), followups AS (
 SELECT c.id conversation_id,c.contact_id,j.key,j.value FROM c CROSS JOIN LATERAL jsonb_each(coalesce(c.custom_attributes->'scenario_followups','{}')) j
 WHERE j.value->>'screen' IN('s5','s52','s59','s7') AND coalesce(j.value->>'test_only','false')<>'true'
), followup_events AS (
 SELECT j.contact_id,'followup_sent' metric,CASE j.value->>'screen' WHEN 's59' THEN 'reminder' WHEN 's7' THEN 'consultation' ELSE 'checkin' END category
 FROM followups j WHERE EXISTS (SELECT 1 FROM m WHERE m.conversation_id=j.conversation_id AND m.attrs->>'bot_turn_key'='scenario-followup:'||j.key AND m.outgoing AND m.status<>3 AND coalesce(m.source_id,'')<>'')
), ev AS (
 SELECT * FROM button_events WHERE category IS NOT NULL UNION ALL SELECT * FROM cert_events UNION ALL SELECT * FROM request_events WHERE category IS NOT NULL UNION ALL SELECT * FROM followup_events
), cohorts AS (
 SELECT 'all' cohort,contact_id FROM clients UNION ALL SELECT 'native',contact_id FROM clients WHERE NOT imported
), counts AS (
 SELECT co.cohort,e.metric,e.category,count(DISTINCT co.contact_id) n FROM cohorts co JOIN ev e USING(contact_id) GROUP BY 1,2,3
 UNION ALL
 SELECT co.cohort,e.metric,'_total',count(DISTINCT co.contact_id) FROM cohorts co JOIN ev e USING(contact_id) GROUP BY 1,2
)
SELECT json_build_object(
 'checked_at',now(),'read_only',current_setting('transaction_read_only'),
 'scope_verified',(SELECT count(*)=1 FROM scope),
 'first_message',(SELECT min(created_at) FROM m WHERE contact_id IN(SELECT contact_id FROM clients)),
 'last_message',(SELECT max(created_at) FROM m WHERE contact_id IN(SELECT contact_id FROM clients)),
 'excluded_contacts',(SELECT count(*) FROM test_contacts),
 'cohorts',(SELECT json_agg(row_to_json(q)) FROM(SELECT cohort,count(*) contacts FROM cohorts GROUP BY 1 ORDER BY 1) q),
 'counts',(SELECT json_agg(row_to_json(q)) FROM(SELECT * FROM counts ORDER BY 1,2,3) q),
 'quality',json_build_object(
   'menu_has_only_need_buttons',(SELECT count(*)=0 FROM incoming x WHERE x.cb NOT LIKE 'need:%' AND EXISTS(SELECT 1 FROM menu p WHERE p.conversation_id=x.conversation_id AND (x.attrs->>'in_reply_to'=p.id::text OR x.attrs->>'in_reply_to_external_id'=p.source_id))),
   'followup_unconfirmed_contacts',(SELECT count(DISTINCT j.contact_id) FROM followups j JOIN clients cl USING(contact_id) WHERE j.value->>'sent_at' IS NOT NULL AND NOT EXISTS(SELECT 1 FROM m WHERE m.conversation_id=j.conversation_id AND m.attrs->>'bot_turn_key'='scenario-followup:'||j.key AND m.outgoing AND m.status<>3 AND coalesce(m.source_id,'')<>''))
 ));
