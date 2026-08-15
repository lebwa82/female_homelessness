# Nevidimiy Fund Telegram Agent Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the scripted prototype with a Telegram MVP that follows the approved S01–S19 product flow, runs independent risk and support Qwen agents concurrently, stores auditable aid/escalation/follow-up state in PostgreSQL, and degrades safely.

**Architecture:** aiogram remains a thin Telegram adapter around a channel-neutral `ConversationService`. PydanticAI sends one native structured-output request to `RiskAgent` and one to `SupportAgent` concurrently through Yandex AI Studio; backend policy merges local/model risk, validates the final action, and executes side effects only after the safety result. PostgreSQL 18 stores one persistent conversation per Telegram identity plus messages, agent runs, requests, escalations and recoverable scheduled jobs.

**Tech Stack:** Python 3.14, uv, aiogram 3, PydanticAI slim with OpenAI provider, OpenAI Responses API, Qwen3.6-35B in Yandex AI Studio, Pydantic v2, SQLAlchemy async, asyncpg, PostgreSQL 18, Presidio/spaCy, pytest/pytest-asyncio, ruff, Podman Compose, systemd, just.

## Global Constraints

- Current user channel is Telegram only; Chatwoot is an interface boundary, not a runtime dependency.
- User-facing copy fully plays the role of the fund and contains no test/prototype banner.
- Every finite choice is an inline Telegram button; free text remains accepted at every step.
- Every response exposes `Поговорить с живым человеком` unless that is already the primary action.
- One persistent conversation is reused for each Telegram user.
- Exactly two independent LLM requests start concurrently for each ordinary user text: risk and support.
- No LLM-selected side effect executes until risk classification has completed and backend policy accepts it.
- Critical risk discards the support action; suicide copy includes `8-800-2000-122` exactly.
- Human escalation is simulated with user-facing copy and a database event; no staff chat or Chatwoot call is made.
- Full local message history is retained for 30 days and sent to Yandex only after Presidio masking with `x-data-logging-enabled: false`.
- No name, exact address, age, document, image, voice or video collection is added.
- Artificial aid and knowledge records are structurally real and user-facing; fulfilment integration is outside this MVP.
- Follow-up defaults to 7 days, has one reminder after 48 hours, and both delays are configurable.

---

## File Map

- `app/domain.py`: enums and typed channel/action/risk contracts only.
- `app/catalog.py`: deterministic aid catalog and need-to-offer matrix.
- `app/skills.py`: loads the repository-owned `SKILL.md` files into prompts.
- `app/agents.py`: Yandex/PydanticAI clients, prompts, structured calls and audit.
- `app/safety.py`: high-precision local detector and risk merge policy.
- `app/db.py`: SQLAlchemy tables, schema bootstrap and repository operations.
- `app/service.py`: channel-neutral orchestration, action validation and state transitions.
- `app/ui.py`: stable callback IDs and allowed button sets.
- `app/bot.py`: aiogram command/message/callback rendering and worker lifecycle.
- `app/worker.py`: durable PostgreSQL follow-up and retention loop.
- `skills/*/SKILL.md`: MI/IPS behavioral workflows injected into SupportAgent.
- `knowledge/verified_resources.json`: artificial approved knowledge records with provenance.
- `tests/`: unit, contract and PostgreSQL-free application tests.

### Task 1: Domain contracts, catalog and behavioral skills

**Files:**
- Modify: `app/domain.py`
- Create: `app/catalog.py`
- Create: `app/skills.py`
- Create: `app/ui.py`
- Create: `skills/needs-discovery/SKILL.md`
- Create: `skills/offer-aid/SKILL.md`
- Create: `skills/collect-contact/SKILL.md`
- Create: `skills/crisis-escalation/SKILL.md`
- Create: `skills/verified-information/SKILL.md`
- Create: `skills/follow-up/SKILL.md`
- Create: `skills/level-two-support/SKILL.md`
- Test: `tests/test_domain.py`
- Test: `tests/test_skills.py`

**Interfaces:**
- Produces `RiskLevel`, `RiskAssessment`, `ActionKind`, `AgentAction`, `Choice`, `IncomingMessage`, `AgentTurn`.
- Produces `available_aid_for_need(need: NeedKind) -> tuple[AidItem, ...]`.
- Produces `load_support_skills() -> str` and stable `CallbackAction` values.

- [ ] **Step 1: Write failing tests for the contracts and catalog**

```python
def test_housing_offer_is_bounded_to_catalog() -> None:
    assert [item.id for item in available_aid_for_need(NeedKind.HOUSING)] == [
        "hostel_3_nights", "peer_consultation", "legal_consultation"
    ]

def test_agent_action_rejects_more_than_four_choices() -> None:
    with pytest.raises(ValidationError):
        AgentAction(
            kind=ActionKind.SHOW_CHOICES,
            text="Выберите",
            choices=[Choice(id=str(i), label=str(i)) for i in range(5)],
        )
```

- [ ] **Step 2: Run the focused tests and confirm they fail on missing contracts**

Run: `uv run pytest tests/test_domain.py tests/test_skills.py -q`  
Expected: collection fails because `app.catalog`, `app.skills` and the new types do not exist.

- [ ] **Step 3: Implement strict Pydantic contracts and the eight-item catalog**

```python
class AgentAction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: ActionKind
    text: str = Field(min_length=1, max_length=1200)
    choices: tuple[Choice, ...] = Field(default=(), max_length=4)
    need: NeedKind | None = None
    aid_id: str | None = None
    contact_method: ContactMethod | None = None
```

Encode the exact level-one matrix from the product spec. `CallbackAction` values are short ASCII IDs such as `continue`, `pause`, `need:housing`, `aid:hostel_3_nights`, `contact:current_telegram`, `human`, and `followup:better`.

- [ ] **Step 4: Write the seven concise skill files and loader**

Each skill states when it applies, non-negotiable constraints, S01–S19 reference examples, allowed backend actions and forbidden claims. `load_support_skills()` loads files in a fixed tuple order and fails fast if one is absent.

- [ ] **Step 5: Run focused tests**

Run: `uv run pytest tests/test_domain.py tests/test_skills.py -q`  
Expected: all pass.

### Task 2: Local safety detector and merge policy

**Files:**
- Modify: `app/safety.py`
- Modify: `app/domain.py`
- Test: `tests/test_safety.py`

**Interfaces:**
- Consumes `RiskLevel` and `RiskAssessment`.
- Produces `assess_local_risk(text: str) -> RiskAssessment` and `merge_risk(*assessments: RiskAssessment) -> RiskAssessment`.

- [ ] **Step 1: Add failing golden risk tests**

```python
@pytest.mark.parametrize(("text", "level"), [
    ("я хочу покончить с собой", RiskLevel.CRITICAL),
    ("он сейчас меня бьёт", RiskLevel.CRITICAL),
    ("сегодня мне негде ночевать", RiskLevel.URGENT),
    ("боюсь возвращаться", RiskLevel.CONCERN),
    ("хочу поговорить с человеком", RiskLevel.HUMAN_REQUESTED),
])
def test_local_red_flags(text: str, level: RiskLevel) -> None:
    assert assess_local_risk(text).level is level
```

- [ ] **Step 2: Confirm RED**

Run: `uv run pytest tests/test_safety.py -q`  
Expected: failures for the new levels and functions.

- [ ] **Step 3: Implement conservative semantic categories and precedence**

Use compact normalized phrase lists/regex only as high-precision fallback. Merge precedence is `critical > urgent > human_requested > concern > none`; `unknown` blocks side effects and is handled separately by the service.

- [ ] **Step 4: Confirm GREEN**

Run: `uv run pytest tests/test_safety.py -q`  
Expected: all local and merge cases pass.

### Task 3: Database schema and repositories

**Files:**
- Modify: `app/db.py`
- Create: `tests/test_db_models.py`
- Modify: `tests/test_start.py`

**Interfaces:**
- Produces repository functions `get_or_create_conversation`, `append_message`, `load_history`, `record_agent_run`, `record_risk_assessment`, `record_action`, `create_aid_request`, `create_escalation`, `schedule_followup`, `claim_due_jobs`, `complete_job`, `delete_conversation_data`, `purge_expired_content`.
- `get_or_create_conversation(channel: str, platform_user_id: int, chat_id: int, username: str | None) -> Conversation` always returns the same row for the same channel identity.

- [ ] **Step 1: Write failing model/repository tests**

Tests assert table metadata has `agent_runs`, `risk_assessments`, `aid_requests`, `contact_points`, `escalations`, `followup_jobs`, and that aid idempotency uses a unique request key.

- [ ] **Step 2: Confirm RED**

Run: `uv run pytest tests/test_db_models.py -q`  
Expected: missing-table assertions fail.

- [ ] **Step 3: Add SQLAlchemy models and idempotent bootstrap migrations**

Use typed JSONB audit fields. Store `platform_user_hash` from keyed SHA-256 config and keep Telegram IDs needed for replies. Add indexes on conversation identity, message timestamp, due job status/time and escalation level. Extend `init_db()` with `CREATE TABLE IF NOT EXISTS` through metadata plus explicit `ALTER TABLE ... ADD COLUMN IF NOT EXISTS` for the existing production volume.

- [ ] **Step 4: Implement narrow repository operations**

All writes commit in one repository call. `create_aid_request` writes the request, contact and follow-up job transactionally. `delete_conversation_data` removes messages/contact values/jobs and resets state while retaining one aggregate `data_deleted` event.

- [ ] **Step 5: Confirm GREEN**

Run: `uv run pytest tests/test_db_models.py tests/test_start.py -q`  
Expected: all pass without needing a live DB by testing SQLAlchemy metadata and mocked session contracts.

### Task 4: Two concurrent structured Yandex agents

**Files:**
- Modify: `pyproject.toml`
- Modify: `uv.lock`
- Replace: `app/llm.py` with `app/agents.py`
- Test: `tests/test_agents.py`
- Modify: `scripts/llm_health_check.py`

**Interfaces:**
- Produces protocol `AgentGateway.evaluate(context: AgentContext) -> AgentEvaluation`.
- `YandexAgentGateway.evaluate` starts `classify_risk` and `choose_action` before awaiting either.
- `AgentEvaluation` carries independent outputs and non-sensitive audits.

- [ ] **Step 1: Add PydanticAI dependency**

Run: `uv add "pydantic-ai-slim[openai]"`  
Expected: resolves on Python 3.14 and updates the lockfile.

- [ ] **Step 2: Write contract tests with a fake gateway transport**

```python
async def test_evaluate_starts_exactly_two_calls_concurrently() -> None:
    probe = ConcurrencyProbe()
    gateway = YandexAgentGateway(call=probe)
    result = await gateway.evaluate(context)
    assert probe.started == {"risk", "support"}
    assert probe.max_in_flight == 2
    assert result.risk.level is RiskLevel.NONE
```

Also assert complete history is Presidio-redacted, header logging is disabled, max output is 150, and audits contain no raw prompt or API key.

- [ ] **Step 3: Confirm RED**

Run: `uv run pytest tests/test_agents.py -q`  
Expected: import/contract failures.

- [ ] **Step 4: Implement PydanticAI models and prompts**

```python
client = AsyncOpenAI(
    api_key=settings.yandex_ai_api_key,
    base_url="https://ai.api.cloud.yandex.net/v1",
    project=settings.yandex_cloud_folder_id,
    default_headers={"x-data-logging-enabled": "false"},
)
provider = OpenAIProvider(openai_client=client)
model = OpenAIResponsesModel(settings.model_uri, provider=provider)
risk_agent = Agent(model, output_type=NativeOutput(RiskAssessment))
support_agent = Agent(model, output_type=NativeOutput(AgentAction))
```

Run both with `asyncio.create_task`, stable system instructions, redacted transcript, catalog/knowledge/state context, temperature `0.3`, and `max_tokens=150`. Convert PydanticAI usage/response metadata into `AgentRunAudit` without storing request bodies.

- [ ] **Step 5: Confirm GREEN with mocks, then run a real compatibility smoke**

Run: `uv run pytest tests/test_agents.py -q`  
Expected: all pass.  
Run: `uv run python -m scripts.llm_health_check --structured`  
Expected: `risk=none`, a valid `AgentAction`, exactly two successful request audits, and no prompt/token output.

If the Yandex endpoint rejects a provider-specific PydanticAI field, keep the same `AgentGateway` but implement the two requests with `AsyncOpenAI.responses.create` plus strict JSON schema and Pydantic validation. The acceptance condition remains two real structured Responses calls.

### Task 5: Conversation application service and policy gate

**Files:**
- Rewrite: `app/service.py`
- Modify: `app/knowledge.py`
- Modify: `knowledge/verified_resources.json`
- Test: `tests/test_service.py`
- Test: `tests/test_product_scenarios.py`

**Interfaces:**
- Consumes repositories, `AgentGateway`, catalog, knowledge and safety functions.
- Produces `ConversationService.start(identity) -> AgentTurn`, `handle_text(identity, text) -> AgentTurn`, and `handle_action(identity, callback_id) -> AgentTurn`.

- [ ] **Step 1: Write failing product scenario tests**

Cover S01 pause/continue, five needs, free-text need, one aid selection, city/region gating, all S10 contact methods, second request, direct human request, critical override, insufficient legal knowledge, S15–S19, opt-outs and stale callback.

```python
async def test_critical_risk_discards_support_side_effect(service, gateway, repo) -> None:
    gateway.result = evaluation(
        risk=RiskAssessment(level="critical", categories=["suicide"]),
        action=AgentAction(
            kind="create_aid_request",
            aid_id="food_card",
            text="Оформляю карточку на продукты",
        ),
    )
    turn = await service.handle_text(identity, "не хочу жить")
    assert "8-800-2000-122" in turn.text
    assert repo.aid_requests == []
    assert repo.escalations[-1].level == "critical"
```

- [ ] **Step 2: Confirm RED**

Run: `uv run pytest tests/test_service.py tests/test_product_scenarios.py -q`  
Expected: failures because the current scripted service has no typed action gate.

- [ ] **Step 3: Implement deterministic callback transitions**

Callbacks never need an LLM call: they resolve known actions, update state and return approved screens. Concrete options always become `Choice` records, and the human choice is appended centrally.

- [ ] **Step 4: Implement free-text orchestration and safety policy**

Append the user message, build 30-day context, start both agent requests, merge risk, persist both run audits/risk, then either return critical/unknown fallback or validate and execute the support action. Reject catalog IDs not present in the injected catalog and knowledge claims without an approved article.

- [ ] **Step 5: Replace knowledge seed with provenance-aware artificial records**

Each record contains `id`, `topics`, `region`, `status`, `owner`, `source_title`, `source_url`, `verified_at`, `expires_at`, and `text`. Retrieval returns only approved, non-expired records and formats citation plus verification date.

- [ ] **Step 6: Confirm GREEN**

Run: `uv run pytest tests/test_service.py tests/test_product_scenarios.py -q`  
Expected: all S01–S19 and safety-policy cases pass.

### Task 6: aiogram inline-button adapter and text-only fallback

**Files:**
- Rewrite: `app/bot.py`
- Modify: `app/ui.py`
- Rewrite: `tests/test_start.py`
- Rewrite: `tests/test_ui.py`
- Create: `tests/test_bot_handlers.py`

**Interfaces:**
- Consumes `ConversationService` and `AgentTurn`.
- Produces aiogram handlers for `/start`, `/delete`, `/system_info`, text, callback and unsupported content.

- [ ] **Step 1: Write failing rendering and handler tests**

Assert inline callback IDs, human button on every finite-choice turn, no test banner, callbacks are answered, unsupported media gets the text-only copy, and `/system_info` keeps non-secret build metadata.

- [ ] **Step 2: Confirm RED**

Run: `uv run pytest tests/test_start.py tests/test_ui.py tests/test_bot_handlers.py -q`  
Expected: current reply-keyboard/banner behavior fails.

- [ ] **Step 3: Implement thin handlers and inline rendering**

```python
def render_keyboard(turn: AgentTurn) -> InlineKeyboardMarkup | None:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=c.label, callback_data=c.id)] for c in turn.choices
        ]
    ) if turn.choices else None
```

The bot adapter passes Telegram identity/message metadata to the service, stores the assistant response through the service, and never contains product branching.

- [ ] **Step 4: Confirm GREEN**

Run: `uv run pytest tests/test_start.py tests/test_ui.py tests/test_bot_handlers.py -q`  
Expected: all pass.

### Task 7: Follow-up, retention and lifecycle

**Files:**
- Create: `app/worker.py`
- Modify: `app/bot.py`
- Modify: `app/config.py`
- Create: `tests/test_worker.py`
- Modify: `tests/test_config.py`

**Interfaces:**
- Produces `run_due_jobs(bot, repo, now) -> int`, `followup_loop(bot, repo)`, and `retention_loop(repo)`.
- Adds settings `followup_delay_seconds=604800`, `followup_reminder_seconds=172800`, `message_retention_days=30`, `worker_poll_seconds=15`, `identity_hash_key`.

- [ ] **Step 1: Write failing worker/config tests**

Assert due S15 is sent once, retry after process failure remains due, reminder is scheduled only when unanswered, answer cancels reminder, and retention purges content older than 30 days.

- [ ] **Step 2: Confirm RED**

Run: `uv run pytest tests/test_worker.py tests/test_config.py -q`  
Expected: missing settings and worker functions.

- [ ] **Step 3: Implement PostgreSQL-backed jobs and lifecycle tasks**

Use `FOR UPDATE SKIP LOCKED` semantics in the real repository and an injectable clock in tests. Start worker tasks after `init_db`; cancel/await them in `bot.main()` cleanup so systemd restarts cleanly.

- [ ] **Step 4: Confirm GREEN**

Run: `uv run pytest tests/test_worker.py tests/test_config.py -q`  
Expected: all pass.

### Task 8: Full verification, documentation and deployment

**Files:**
- Modify: `README.md`
- Modify: `justfile`
- Modify: `scripts/deploy_prod.sh`
- Create: `scripts/scenario_smoke.py`
- Modify: `docs/superpowers/plans/2026-08-15-nevidimiy-fond-telegram-agent.md`

**Interfaces:**
- Produces `just scenario-smoke`, preserves `just run`, `just check`, `just llm-health`, and `just deploy-prod`.

- [ ] **Step 1: Add a deterministic scenario smoke command**

The script exercises start → food → food card → current Telegram contact → second-help question, plus a separate suicidal message asserting hotline and no aid side effect. It uses a fake channel/gateway by default and `--live-llm` for Yandex compatibility.

- [ ] **Step 2: Update operator documentation**

Document skills/actions, exact two-call behavior/cost implication, artificial catalog/KB, configurable minute-scale follow-up test, text-only limitation, hidden system info, privacy/retention, safe logs, VM commands and pre-pilot blockers. Do not add a user-facing test banner.

- [ ] **Step 3: Run local verification**

Run: `just check`  
Expected: ruff and all tests pass.  
Run: `just scenario-smoke`  
Expected: both deterministic end-to-end scenarios pass.  
Run: `just llm-health`  
Expected: two structured Qwen calls succeed without printing prompts/secrets.

- [ ] **Step 4: Inspect the final diff and request code review**

Run: `git diff --check` and `git status --short`. Review all changed files against the design, with special attention to critical override, double calls, buttons, retention and user-facing copy.

- [ ] **Step 5: Commit and push**

Stage only repository files, create a descriptive feature commit on `main`, then push `main` to `origin`.

- [ ] **Step 6: Deploy and verify production**

Run: `just deploy-prod lebwa82@89.169.180.0`  
Expected: clean committed archive deployed, dependencies locked, systemd active, all server checks pass. Then verify `/system_info` build metadata, Telegram Bot API identity, PostgreSQL connectivity and safe recent logs without reading secret values.

## Plan Self-Review

- Spec coverage: S01–S19, buttons/free text, aid matrix, two agents, risk levels, contacts, one-request semantics, persistent conversation, knowledge, follow-up, retention, deletion, text-only input, metrics/audit and future Chatwoot boundary each map to a task.
- Type consistency: `RiskAssessment`, `AgentAction`, `AgentTurn`, `ConversationService` and repository method names are defined once and consumed unchanged in later tasks.
- Scope exclusions are explicit: real fulfilment, staff handoff, Chatwoot, non-Telegram channels, media handling, age policy and external-pilot controls are not hidden implementation gaps.
