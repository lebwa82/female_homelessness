-- Synthetic Chatwoot source fixtures for an isolated local database.
-- Scope account 3, inbox 5, exclude contact 8; approved label title: approved.

CREATE TABLE inboxes (
    id bigint PRIMARY KEY,
    account_id bigint,
    channel_type text,
    csat_survey_enabled bool,
    lock_to_single_conversation bool,
    updated_at timestamp
);

CREATE TABLE contacts (
    id bigint PRIMARY KEY,
    account_id bigint,
    created_at timestamp,
    updated_at timestamp
);

CREATE TABLE contact_inboxes (
    id bigint PRIMARY KEY,
    contact_id bigint,
    inbox_id bigint,
    created_at timestamp,
    updated_at timestamp
);

CREATE TABLE conversations (
    id bigint PRIMARY KEY,
    account_id bigint,
    inbox_id bigint,
    contact_id bigint,
    contact_inbox_id bigint,
    created_at timestamp,
    updated_at timestamp,
    status int,
    status_changed_at timestamp,
    assignee_id bigint,
    assignee_agent_bot_id bigint,
    ai_assignee_type text,
    custom_attributes jsonb
);

CREATE TABLE messages (
    id bigint PRIMARY KEY,
    account_id bigint,
    conversation_id bigint,
    inbox_id bigint,
    sender_id bigint,
    sender_type text,
    created_at timestamp,
    updated_at timestamp,
    message_type int,
    private bool,
    status int
);

CREATE TABLE users (
    id bigint PRIMARY KEY,
    type text,
    updated_at timestamp
);

CREATE TABLE account_users (
    user_id bigint,
    account_id bigint
);

CREATE TABLE labels (
    id bigint PRIMARY KEY,
    account_id bigint,
    title text,
    updated_at timestamp
);

CREATE TABLE tags (
    id bigint PRIMARY KEY,
    name text
);

CREATE TABLE taggings (
    id bigint PRIMARY KEY,
    tag_id bigint,
    taggable_type text,
    taggable_id bigint,
    context text,
    created_at timestamp
);

CREATE TABLE csat_survey_responses (
    id bigint PRIMARY KEY,
    account_id bigint,
    conversation_id bigint,
    message_id bigint,
    contact_id bigint,
    rating int,
    created_at timestamp,
    updated_at timestamp
);

CREATE TABLE women_help_certificate_pools (
    id bigint PRIMARY KEY,
    provider text
);

CREATE TABLE women_help_certificate_pool_aids (
    pool_id bigint,
    aid_id text,
    PRIMARY KEY(pool_id, aid_id)
);

CREATE TABLE women_help_pdf_certificates (
    id bigint PRIMARY KEY,
    account_id bigint,
    pool_id bigint,
    contact_id bigint,
    conversation_id bigint,
    source_message_id bigint,
    chatwoot_message_id bigint,
    nominal_rubles int,
    selected_aid_id text,
    status text,
    record_kind text,
    is_test bool,
    valid_from timestamptz,
    expires_at timestamptz,
    reserved_at timestamptz,
    submitted_at timestamptz,
    delivered_at timestamptz,
    accepted_at timestamptz,
    issued_on date
);

CREATE TABLE women_help_certificate_requests (
    id bigint PRIMARY KEY,
    account_id bigint,
    contact_id bigint,
    conversation_id bigint,
    source_message_id bigint,
    last_conversation_id bigint,
    last_source_message_id bigint,
    aid_id text,
    pool_id bigint,
    reason text,
    status text,
    first_requested_at timestamptz,
    last_requested_at timestamptz,
    fulfilled_certificate_id bigint,
    source_event_keys jsonb
);

INSERT INTO inboxes (id, account_id, channel_type) VALUES
    (5, 3, 'Channel::Telegram'), (6, 4, 'Channel::Telegram'), (7, 3, 'Channel::Api');
INSERT INTO contacts (id, account_id) VALUES (8, 3), (9, 3), (10, 4), (11, 3);
INSERT INTO contact_inboxes (id, contact_id, inbox_id) VALUES
    (1, 8, 5), (2, 9, 5), (3, 10, 5), (4, 9, 7), (5, 11, 7);
INSERT INTO conversations (
    id, account_id, inbox_id, contact_id, contact_inbox_id, created_at, updated_at,
    status, custom_attributes
) VALUES
    (1, 3, 5, 8, 1, '2020-01-01', '2026-11-27', 0, '{}'),
    (2, 3, 5, 9, 2, '2020-01-01', '2026-11-27', 1, '{
        "scenario_requests": {
            "req-1": {
                "state": "completed",
                "aid_id": "legal_consultation",
                "requested_at": "2026-11-27T10:00:00+00:00",
                "extra": "not allowed"
            },
            "wrong": [{"description": "private data"}],
            "invalid key": "payload"
        },
        "scenario_followups": {
            "check:req-1": {
                "state": "pending",
                "screen": "s5",
                "context": {
                    "aid_id": "legal_consultation",
                    "request_key": "req-1",
                    "extra": "not allowed"
                }
            }
        }
    }'),
    (3, 4, 6, 10, 3, '2020-01-01', '2026-11-27', 0, '{}'),
    (4, 3, 7, 9, 4, '2020-01-01', '2026-11-27', 0, '{}');
INSERT INTO messages (
    id, account_id, conversation_id, inbox_id, created_at, updated_at, message_type,
    private, sender_type, sender_id, status
) VALUES
    (1, 3, 2, 5, '2020-01-01', '2026-11-27', 0, false, 'Contact', 9, 0),
    (2, 3, 2, 5, '2020-01-01', '2020-01-01', 1, false, 'User', 1, 0),
    (3, 4, 2, 5, '2020-01-01', '2026-11-27', 0, false, 'Contact', 10, 0),
    (4, 3, 2, 7, '2020-01-01', '2026-11-27', 0, false, 'Contact', 11, 0),
    (5, 3, 1, 5, '2020-01-01', '2026-11-27', 0, false, 'Contact', 8, 0);
INSERT INTO users (id) VALUES (1), (2);
INSERT INTO account_users (user_id, account_id) VALUES (1, 3), (2, 4);
INSERT INTO labels (id, account_id, title) VALUES
    (1, 3, 'approved'), (2, 4, 'approved'), (3, 3, 'unapproved');
INSERT INTO tags (id, name) VALUES (1, 'approved'), (2, 'approved'), (3, 'unapproved');
INSERT INTO taggings (id, tag_id, taggable_type, taggable_id, context) VALUES
    (1, 1, 'Conversation', 2, 'labels'), (2, 1, 'Contact', 9, 'labels'),
    (3, 2, 'Conversation', 1, 'labels'), (4, 2, 'Conversation', 3, 'labels'),
    (5, 3, 'Conversation', 2, 'labels');
INSERT INTO csat_survey_responses (id, account_id, conversation_id, message_id, contact_id, rating)
VALUES (1, 3, 2, 1, 9, 5), (2, 4, 3, 1, 10, 4), (3, 3, 1, 1, 8, 5);
INSERT INTO women_help_certificate_pools (id, provider) VALUES (1, 'pyaterochka'), (2, 'ozon');
INSERT INTO women_help_certificate_pool_aids (pool_id, aid_id) VALUES
    (1, 'children_card'), (1, 'food_card'), (2, 'medicine_card');
INSERT INTO women_help_pdf_certificates (
    id, account_id, pool_id, contact_id, conversation_id, source_message_id,
    chatwoot_message_id, record_kind, status, is_test, selected_aid_id
) VALUES
    (1, NULL, 1, NULL, NULL, NULL, 2, 'certificate', 'delivered', false, 'food_card'),
    (2, NULL, 2, NULL, NULL, NULL, NULL, 'certificate', 'available', false, NULL),
    (3, 3, 1, 9, 2, 1, NULL, 'historical_issuance', 'accepted', false, 'food_card'),
    (4, 4, 2, 10, 3, 1, NULL, 'historical_issuance', 'accepted', false, 'medicine_card'),
    (5, NULL, 2, NULL, NULL, NULL, 3, 'certificate', 'delivered', false, 'medicine_card');
INSERT INTO women_help_certificate_requests (
    id, account_id, contact_id, conversation_id, source_message_id,
    last_conversation_id, last_source_message_id, pool_id, aid_id, reason, status,
    first_requested_at, last_requested_at, source_event_keys
) VALUES
    (1, 3, 9, 2, 1, 4, 99, 1, 'food_card', 'out_of_stock', 'waiting',
     '2026-11-01T10:00:00Z', '2026-11-27T11:00:00Z', '["req-1", "out-of-scope"]'),
    (2, 4, 10, 3, 1, 3, 1, 2, 'medicine_card', 'paused', 'waiting',
     '2026-11-01T10:00:00Z', '2026-11-27T11:00:00Z', '["req-2"]'),
    (3, 3, 9, 2, 1, 2, 2, 1, 'food_card', 'out_of_stock', 'waiting',
     '2026-11-01T10:00:00Z', '2026-11-27T11:00:00Z', '["req-3", "req-3"]'),
    (4, 3, 9, 2, 1, 2, 2, 1, 'food_card', 'out_of_stock', 'waiting',
     '2026-11-01T10:00:00Z', '2026-11-27T11:00:00Z',
     '["req-4", {"key": "private"}, 123, "text with spaces"]'),
    (5, 3, 9, 4, 99, 2, 2, 1, 'food_card', 'out_of_stock', 'waiting',
     '2026-11-01T10:00:00Z', '2026-11-27T11:00:00Z', '["out-of-scope", "req-5"]'),
    (6, 3, 9, 2, 1, 2, 2, 1, 'food_card', 'out_of_stock', 'waiting',
     '2026-11-01T10:00:00Z', '2026-11-27T11:00:00Z', '"invalid root"');
