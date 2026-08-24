# SDD ledger — plan: docs/superpowers/plans/2026-08-21-open-conversation-policy.md

Branch start: `897869f`
Worktree: `/Users/lebwa82/female_homelessness/.worktrees/open-conversation-policy`
Baseline: `uv run pytest -q` → 70 passed.

## Preflight interface scan

| Tasks | Producer → consumer | Finding |
|---|---|---|
| 1 → 2 | `SupportPlan`, risk enum → agent output/parser | Compatible; Task 2 replaces the old action payload. |
| 1 → 3 | domain policy types → `resolve_turn` | Compatible; enums and Pydantic types are sufficient. |
| 1 → 4 | `EscalationRequest` → store/DB | Compatible; DB level becomes nullable and cause becomes independent. |
| 1 → 5 | `ResolvedTurn`, causes, state → service | Gap: Task 1 text does not explicitly add `ConversationState.OPEN_CONVERSATION`, while Task 5 consumes it. Ruling recorded below. |
| 2 → 5 | `AgentEvaluation.plan` → service | Compatible after replacing `action`/`action_audit`. |
| 2 → 6 | gateway typed output → live evaluator | Compatible; evaluator consumes the public gateway boundary. |
| 2 → 7 | health check contract → final live verification | Compatible after renaming support fields. |
| 3 → 5 | `resolve_turn` and `choices_for` → service | Compatible, with a scope clarification: every model-originated UI choice is resolved through `ChoiceSet`; deterministic workflow constants remain owned by `app/ui.py`. |
| 3 → 6 | pure policy → deterministic/live evaluator | Compatible and side-effect free. |
| 4 → 5 | audit and escalation APIs → service | Gap: Task 5 sample records only state-before, but spec requires state-after, rendered callback IDs and executed side effects. Ruling recorded below. |
| 5 → 6 | service behavior → production replay | Compatible; Task 6 adds both pure evaluator and service replay. |
| 5 → 7 | service/smoke behavior → docs/deploy | Compatible once Task 5 extends scenario smoke output. |
| 6 → 7 | eval CLI → just recipes/live acceptance | Compatible; live run is explicitly paid and opt-in at verification time. |

## Per-task internal scan

| Task | Tests vs implementation/files | Finding |
|---|---|---|
| 1 | New domain tests precede enums and safety removal | Internally consistent, subject to OPEN_CONVERSATION ruling. |
| 2 | Payload tests precede SupportPlan parser/prompt | Internally consistent; exactly two concurrent calls retained. |
| 3 | Policy tests precede pure resolver and UI registry | Internally consistent; critical copy owns exact hotline. |
| 4 | Persistence tests precede schema/store changes | Internally consistent; migration is additive except nullable relaxation. |
| 5 | Regression and psychologist tests precede service integration | Internally consistent; old generic fallback must be removed, not patched. |
| 6 | Dataset/loader tests precede evaluator | Internally consistent; 48-case minimum is satisfied by named groups and multi-turn cases. |
| 7 | Recipes/docs follow completed runtime behavior | Internally consistent; production metadata check may require an already-authenticated Telegram client, otherwise verify systemd build metadata without exposing secrets. |

## Preflight rulings

Ruling: Task 1 must add `ConversationState.OPEN_CONVERSATION = "open_conversation"` — the approved spec makes it the top-level default mode and Task 5 depends on it — if wrong, one enum value is extra but harmless.

Ruling: Task 5 must execute the resolved turn before writing the final `policy_decision` audit, and include `state_before`, `state_after`, `risk`, `intent`, `next_action`, `choice_set`, rendered callback IDs, effect/side effects and fallback reason — the approved observability section requires replayable decisions — if wrong, audit metadata grows but carries no raw prompt or new secret.

Ruling: `choices_for` is the sole renderer for model-originated symbolic sets; deterministic workflow screens may use constants defined in `app/ui.py`, never callbacks returned by Qwen — this preserves the one-source-of-truth invariant without rewriting all existing FSM screens — if wrong, a future workflow screen may still require migration into `ChoiceSet`.

Ruling: Task 5 removes legacy `AgentAction`/`ActionKind` and `_apply_model_action` only after all call sites and tests use `SupportPlan`; historical DB rows do not require Python enums to remain — if wrong, an undocumented external importer could break, but the repository has no such public API.

## Task execution

Task 1: dispatched `/root/task1_domain_risk`, base `897869f`.

Task 1: Ruling: keep the suite green by migrating the two legacy service call sites now, without restoring `RiskLevel.HUMAN_REQUESTED` — remove the unreachable risk branch and record the temporary legacy human escalation with `RiskLevel.NONE` plus category `human_requested` until Task 4 introduces `EscalationRequest` — the spec requires human intent not to be a risk and per-task commits must remain testable — if wrong, two product assertions and one temporary audit representation change again in Task 4.

Task 1: review Important: `app/agents.py` risk prompt still permits removed `human_requested`, causing valid model output to parse as UNKNOWN.

Task 1: Ruling: move the narrow risk-prompt cleanup and its regression test from Task 2 into Task 1 — an active producer cannot advertise a value its domain consumer rejects, and Task 2 can build on the corrected prompt — if wrong, Task 2 loses one mechanical edit but no behavior.

Task 1: fix round 1/5 (1 addressed, 0 open; commit `0b747b3`).

Task 1: complete (commits `897869f..0b747b3`, review clean).

Task 2: dispatched `/root/task2_support_plan`, base `0b747b3`.

Task 2: review Critical: `evaluation.action` aliases a `SupportPlan`, then legacy service reads missing `.kind` and crashes on every valid live support response.

Task 2: review Important: custom constructor accepts `Any` and can put `AgentAction` into the typed `plan` field, hiding migration errors.

Task 2: Ruling: remove all `action`/`action_audit` compatibility aliases and the `Any` constructor, migrate repository fixtures to `plan`/`support_audit`, and add a minimal direct SupportPlan consumer in `ConversationService` until Task 5 replaces it with `ConversationPolicy` — the public typed boundary must be true at runtime, not nominal — if wrong, Task 5 will replace a few lines of temporary direct dispatch rather than a legacy adapter.

Task 2: fix round 1/5 (Critical and coverage addressed; runtime type enforcement remains open; commit `0ff50b3`).

Task 2: Ruling: enforce `AgentEvaluation.plan` as `SupportPlan | None` at runtime with a narrow `__post_init__` guard — this boundary receives model output and manually constructed test/adapter values, so annotation-only safety is insufficient after the compatibility bug — if wrong, deliberately invalid callers now fail earlier with TypeError.

Task 2: fix round 2/5 (1 addressed, 0 open; commit `4bb92c3`).

Task 2: complete (commits `0b747b3..4bb92c3`, review clean).

Task 3: dispatched `/root/task3_policy_ui`, base `4bb92c3`.

Task 3: Ruling: inconsistent intent/action combinations resolve to a no-side-effect fallback with `fallback_reason`, rather than inferring handoff or psychologist workflow from one field — the approved policy must validate the model plan before side effects — if wrong, a valid future plan combination may need to be added explicitly.

Task 3: review Important: `state` is discarded, so a consistent new workflow plan can start while another finite workflow is active.

Task 3: Ruling: critical/unknown precedence remains first; a consistent explicit human request and close remain available from any state; while a finite aid/contact/follow-up workflow state is active, policy blocks new OFFER_AID and START_PSYCHOLOGIST_REQUEST effects with `fallback_reason="workflow_active"` — users retain safety/handoff exits without overlapping transactional flows — if wrong, one intentional cross-workflow transition will need an explicit allow-list entry.

Task 3: fix round 1/5 (1 addressed, 0 open; commit `ca15f1b`).

Task 3: complete (commits `4bb92c3..ca15f1b`, review clean).

Task 4: dispatched `/root/task4_persistence_audit`, base `ca15f1b`.

Task 4: Ruling: migrate all active service `create_escalation` callers to `EscalationRequest` in this task and remove the Task 1 NONE-risk bridge, rather than add a permissive store overload — persistence API changes must leave the branch runnable and make cause/risk separation real — if wrong, Task 5 has fewer call-site edits but no contract changes.

Task 4: review Important: `level2:details` persists cause HUMAN_REQUEST instead of LEVEL_TWO_SUPPORT.

Task 4: minor (deferred to Task 5/final review): policy audit exclusion of raw text/prompt is convention-only until the end-to-end service audit test asserts the complete payload keys.

Task 4: minor (deferred to Task 7/final review): PostgreSQL migration has model/DDL coverage but not a live old-row/run-twice integration test.

Task 4: fix round 1/5 (1 addressed, 0 open; commit `0512ff9`).

Task 4: complete (commits `ca15f1b..0512ff9`, review clean; 2 minors deferred).

Task 5: dispatched `/root/task5_service_integration`, base `0512ff9`.

Task 5: Ruling: a tentative psychologist-interest button requires persisted `pending_offer=psychologist`, while an explicit psychologist request can start contact collection directly — this enforces conversational consent without blocking a direct request — if wrong, a tentative reply may need a second model clarification instead of suppressing the button.

Task 5: Ruling: write `policy_decision` audit after execution and allow only structured classification/transition/button fields, excluding user text, assistant text, prompt, plan text and history — this resolves the Task 4 privacy/audit minor and makes turns replayable — if wrong, debugging may require joining the separately retained messages table.

Task 5: review Critical: stale recognized `continue`/`more_help` callbacks can abandon an active request and show the generic menu; repeated human callback duplicates escalations.

Task 5: review Important: finite workflow and concern/urgent side effects bypass `_execute_resolved_turn`; side-effect UI is rendered outside the resolved choice_set; audits report `choice_set=none` for contact buttons and include non-schema workflow keys.

Task 5: Ruling: normalize every text turn, including deterministic workflow inputs and concern/urgent escalation, into a `ResolvedTurn` whose primary effect, additional side effects and final choice_set are complete before `_execute_resolved_turn`; the executor returns the final AgentTurn and audit is derived from that same normalized object — this implements the approved single-source contract — if wrong, ResolvedTurn gains a few workflow-only enum values that could instead live in a separate WorkflowDecision type.

Task 5: Ruling: the audit schema is strict for every text turn; remove `decision_source` and `workflow_transition`, represent workflow execution through `effect`/`side_effects`, and retain exactly the approved structured keys — this favors comparable replay records over extra provenance fields — if wrong, provenance must later be added as an approved schema version.

Task 5: Ruling: callback idempotency is keyed by conversation + callback ID + originating Telegram message ID; the same button message cannot repeat a side effect, while a later button from another message can — this matches Telegram callback semantics — if wrong, channels without stable message IDs need a separate idempotency token.

Task 5: fix round 1/5 (stale/duplicate/UI/audit findings addressed; follow-up pre-transition and callback failure recovery remain open; commit `63c6029`).

Task 5: Ruling: a text received in FOLLOWUP_SENT adds a `complete_followup` resolved side effect; cancellation and state transition occur inside `_execute_resolved_turn`, so audit state_before remains the receipt state — this keeps every text side effect behind one executor — if wrong, follow-up reply interpretation may need its own primary effect rather than an additive one.

Task 5: Ruling: callback idempotency records have processing/completed/failed lifecycle with a bounded lease; claim is marked completed only after all downstream work succeeds, exceptions mark failed, and failed/expired processing claims are reclaimable — this prevents duplicate success while preserving retry after an application/process failure — if wrong, concurrent long-running callbacks could require a longer configurable lease.

Task 5: fix round 2/5 (report schema corrected; resolved follow-up ordering and claim lifecycle implemented, but Postgres record state and partial-effect dedupe remain open; commit `974950c`).

Task 5: Ruling: `ConversationStore.update` mutates the supplied `ConversationRecord` and returns that same object in both implementations — service helpers commonly ignore the return value and the in-memory store already defines this behavior, so parity prevents stale audit state — if wrong, the store protocol should instead force every caller to thread a replacement record.

Task 5: Ruling: derive a stable callback-origin idempotency key from conversation + callback ID + Telegram source message ID and persist it on externally meaningful created effects (escalations and aid requests); retries may re-run orchestration but return the existing effect — this closes the gap between callback claim and independently committed effects without a cross-repository transaction — if wrong, a future new callback side effect must also opt into the same key contract.

Task 5: fix round 3/5 (2 addressed, 0 open; commit `0622901`).

Task 5: complete (commits `0512ff9..0622901`, review clean).

Task 6: dispatched `/root/task6_behavior_evals`, base `0622901`.

Task 6: Ruling: deterministic fixture outputs are independent JSON fields from expected invariants; evaluator must parse the fixture payload rather than synthesize a passing result from expected — this keeps offline replay capable of catching policy regressions — if wrong, the dataset schema is slightly more verbose.

Task 6: review Important: expected risk/intent/choice_set/effect accept arbitrary strings instead of domain enum values.

Task 6: review Important: prod-listen service replay duplicates history instead of loading the exact versioned dataset row, allowing drift.

Task 6: review Minor included in fix: fixture IDs must match dataset IDs exactly; surplus fixture rows are rejected with missing rows.

Task 6: fix round 1/5 (3 addressed, 0 open; commit `55b46f3`).

Task 6: complete (commits `0622901..55b46f3`, review clean).

Task 7: Ruling: defer `just deploy-prod` and production `/system_info` verification until the broad whole-branch review is clean and the feature branch is integrated — deployment is an external side effect and must not precede the final review gate — if wrong, release happens later than the plan's task-local ordering but with a safer artifact.

Task 7: Ruling: source the existing project `.env` only inside the live-check shell without reading or printing it; report aggregate case IDs/classifications only — this uses the already authorized model credentials without exposing secrets — if wrong, live acceptance must be run manually by the user.

Task 7: dispatched `/root/task7_final_acceptance`, base `55b46f3`.

Task 7: live acceptance remained red after four full 53-case runs, including two final runs at temperature 0 after schema normalization (7 cases/19 fields and 5/16). PostgreSQL live assurance was safely blocked by an unavailable local Podman socket. Commit `0c9b34d`.

Task 7: review Critical: the live evaluator calls gateway + `resolve_turn` directly with a hard-coded state and bypasses the production `ConversationService` state/pending-offer/workflow/effect path, so even a green run would not prove deployed behavior.

Task 7: review Important: the design document calls `rationale_short` canonical while runtime calls `rationale` canonical and accepts `rationale_short` only as provider compatibility.

Task 7: Ruling: do not accept Task 7 or deploy on a random green model run; add a deterministic policy-kernel corrective plan whose live evaluator replays the actual service path — the current gate is both unstable and not runtime-faithful — if wrong, release is delayed while the evaluation and control boundary are strengthened.

Corrective plan: `docs/superpowers/plans/2026-08-21-deterministic-policy-kernel.md`, commit `df515d3`.

Corrective plan: Ruling: retain exactly two concurrent model calls but make their outputs diagnostic-only; only versioned local signals plus backend state may authorize buttons, effects, transitions, requests, and escalations — model enum variability has empirically changed product behavior across identical runs — if wrong, the local grammar may miss an unseen paraphrase, but the permanent human button and open-conversation fallback remain available and rule coverage can be reviewed/versioned.

Corrective plan: Ruling: separate deploy-blocking hard behavioral projections from model diagnostic deltas while preserving all 53 histories and expected semantic diagnostics — this strengthens the release contract around actual UI/state/side effects rather than demanding impossible model-label determinism — if wrong, a semantic regression could appear first as a diagnostic alert instead of blocking release, while hard behavior remains invariant.

Corrective plan: Ruling: generic aid interest with no known need resolves to backend `need_categories`, not a fabricated aid catalog — the approved policy already supports a category choice and catalog contents require a typed need — if wrong, one dataset case and its deterministic UI expectation change from a direct catalog to a category screen.

Task 8: dispatched `/root/task8_signals`, base `df515d3`; implementation commit `868f372`.

Task 8: review Critical: bare `не хочу жить` matches contextual residence/relationship phrases and would cause false CRITICAL escalation.

Task 8: review Important: bare aid topic nouns authorize transactions without request evidence; token spans can be empty/reversed; a non-immediate ongoing threat loses concern routing.

Task 8: review Minor: four expected-open multi/psychologist rows lack explicit negative coverage; report test count is stale.

Task 8: Ruling: require bounded request/lack context for aid nouns, restrict suicide grammar to existential forms rather than residence/relationship continuations, enforce `token_end > token_start` in the domain model, and emit concern for threat without immediate markers — these are correctness properties of the deterministic authorization boundary — if wrong, the matcher becomes intentionally more conservative and falls back to open conversation rather than performing an unsafe hard action.

Task 8: fix round 1/5 (all Critical/Important/Minor findings addressed; commit `422041c`).

Task 8: complete (commits `df515d3..422041c`, re-review clean; 226 tests).

Task 9: Ruling: evolve the strict policy audit to a versioned v2 allow-list with matcher/policy versions, local rule IDs, and diagnostic labels/statuses, while removing model-owned `next_action` — the ownership boundary changed and replay now needs to distinguish deterministic evidence from probabilistic diagnostics — if wrong, downstream log consumers expecting the v1 exact key set must be migrated.

Task 9: Ruling: a model may persist only a soft `suggested_support=psychologist` pending offer and may not render a psychologist button or start a workflow in that turn; a later deterministic current-user signal plus pending context authorizes UI/workflow — this preserves conversational suggestions without giving the model direct transactional control — if wrong, spontaneous psychologist suggestions become diagnostic-only and require an explicit user mention to proceed.

Task 9: Ruling: provider risk labels are stored as diagnostics and cannot create hard UI/state/escalation; reviewed local rules own immediate product behavior, while unmatched model risk remains observable for rule-review expansion — this makes hard behavior reproducible but deliberately trades open-world automatic crisis routing for a versioned safety grammar in the MVP — if wrong, stakeholders must explicitly accept probabilistic model-only safety overrides.

Task 9: dispatched `/root/task9_policy_kernel`, base `c89d196`.

Task 9: implementation commit `1a2cfc5`; review Important: crisis/human executor routes retain stale psychologist pending offer; draft guard misses common completed-action grammatical variants; deterministic human grammar misses explicit desire-to-speak with an external human role.

Task 9: review Minor: a policy test infers pending offer from assistant text rather than explicit persisted context.

Task 9: Ruling: every critical/handoff route clears soft contextual offers; explicit desire-to-speak + external human role is a hard human signal, but human-like conversation wording without an external role remains a near miss; guarded drafts reject bounded families of connected/registered/sent/accepted external-action claims — these close stale authorization and false-action boundaries without letting model intent control behavior — if wrong, some legitimate conversational draft will fall back to canonical safe copy.

Task 9: fix round 1/5 (stale offer, explicit human grammar, initial draft guard, fixture context addressed; commit `bc68691`; one guard-window Important remains open).

Task 9: fix round 2/5 (remaining completion-claim guard window addressed; commit `b357ca6`).

Task 9: complete (commits `c89d196..b357ca6`, re-review clean; 237 tests).

Task 10: Ruling: seed service replay with explicit persisted runtime context rather than infer it from assistant prose; histories remain byte-for-byte unchanged — state/pending offer are product data, not language-model semantics — if wrong, the dataset gains metadata that duplicates a state reconstructable only by replaying every prior external callback.

Task 10: Ruling: live exit status is blocked by hard behavior or provider health failure, while diagnostic label differences are reported separately and retained for model/rule quality review — model labels are no longer product actions, so treating their nondeterminism as a UI regression would recreate the old boundary error — if wrong, a diagnostic-quality threshold must be added as a separate release metric.

Task 10: dispatched `/root/task10_runtime_eval`, base `b357ca6`.

Task 10: implementation commit `a125b11`; local 248 tests/offline 53 hard-clean. External live runs pending renewed approval; local Podman PG assurance blocked by runtime socket.

Task 10: review Important: hard behavior expectations do not assert exact rule IDs and canonical copy is asserted only for the hotline; PG assurance does not insert/read a rolled-back historical row; provider JSON parser accepts duplicate keys.

Task 10: Ruling: every dataset case explicitly expects exact rule IDs (including an empty set), and every backend-owned hard route expects a stable canonical-copy fragment while free open conversation may keep no copy assertion — this detects rule drift/canonical-action regressions without freezing LLM prose — if wrong, benign rule-ID renames require versioned dataset migration.

Task 10: Ruling: strict provider JSON rejects duplicate members and non-standard constants before Pydantic validation; PG assurance inserts and reads a historical level only inside a rolled-back transaction — these make both claimed boundaries executable rather than nominal — if wrong, a provider duplicate-key response that could have been deterministically last-wins becomes invalid diagnostic fallback.

Task 10: fix round 1/5 (rule IDs/canonical copy, rollback historical read, strict JSON addressed; commit `48bee95`; re-review clean; 255 tests).

Task 10: external live run 1: 53 cases, 0 hard failures, 23 diagnostic deltas, 8 provider failures.

Task 10: external live run 2: 53 cases, 0 hard failures, 23 diagnostic deltas, 9 provider failures.

Task 10: Ruling: hard behavior acceptance passed reproducibly, but deployment remains blocked until provider failures are classified and reduced to the explicit health threshold; do not hide invalid/unavailable diagnostics inside aggregate deltas — safe error-type/schema-field aggregation is permitted, never raw model output — if wrong, the deterministic fallback is already safe but conversational quality is released later than necessary.

Task 10: fix round 2/5 (safe finite provider failure aggregation; commit `0b7bd1a`; re-review clean; 257 tests).

Task 10: diagnostic external live run 3: 53 cases, 0 hard failures, 22 diagnostic deltas, 9 provider failures; all 9 were JSON objects with no transport errors. Safe causes: safety rationale `string_too_long` 4; support intent enum 5; support need_hint enum 2.

Task 10: Ruling: preserve a usable diagnostic response when only non-authoritative metadata is malformed: truncate overlong safety rationale to its stored bound, normalize unknown support intent/need hint to `None`, audit only finite normalization categories, and keep those values visible as diagnostic deltas — draft text and local deterministic behavior remain usable and no invalid enum can authorize product action — if wrong, strict whole-object rejection remains safer but causes avoidable generic fallbacks for otherwise usable replies.

Task 10: fix round 3/5 (partial diagnostic normalization; commit `35d6b9a`; re-review clean; 271 tests).

Task 10: final external live gate run 1 on `35d6b9a`: 53 cases, 0 hard failures, 0 provider failures, 27 diagnostic deltas.

Task 10: final external live gate run 2 on `35d6b9a`: 53 cases, 0 hard failures, 0 provider failures, 26 diagnostic deltas.

Task 10: Ruling: accept Task 10's model/service gate because two sequential runs have zero hard and provider failures; retain diagnostic deltas as a non-authoritative quality metric rather than a release blocker — deterministic backend behavior is invariant and diagnostic drift is explicitly observable — if wrong, add a separately approved diagnostic accuracy threshold before a wider pilot.

Final whole-branch review: FAIL at `eb4e14f` with 3 Critical, 11 Important, and 4 Minor findings. Reproduced release blockers: Telegram handles cross the Yandex boundary unredacted; direct suicidal statements miss the deterministic crisis route; unrelated negation suppresses active-violence signals. Important findings cover explicit-human grammar, transactional negation, finite-workflow cancellation/cleanup, local-safety ordering, false external-action claims, per-update/conversation idempotency, deletion, retention, persisted audit allow-listing, deployment gates, and stale documentation. Minor findings cover evaluator soft-state integrity, `/system_info` human-button consistency, duplicate escalation indexes, and stale evidence counts.

Final review: Ruling: treat the three reproduced privacy/safety findings and all correctness/privacy/release Important findings as load-bearing and fix them in one security-focused TDD round before integration; do not deploy or push the branch meanwhile — the bot is intended for vulnerable users and model diagnostics are deliberately non-authoritative — if wrong, the release is delayed while MVP hard boundaries become stricter than necessary.

Final review: Ruling: contact collection must persist a typed contact locally while every current and historical model view substitutes `[CONTACT]`; Telegram handles also receive a local recognizer, and Presidio must not refresh external PSL data at runtime — this prevents off-host disclosure without changing the authorized local storage contract — if wrong, some benign `@...` text may be over-redacted from model context.

Final review: Ruling: clause-aware polarity is a shared deterministic primitive for suicide, violence, aid, psychologist, eviction, and threat signals; direct death/self-harm intent and help-seeking continuations route critical, while residence/relationship and true predicate negations remain near misses — this fixes both false negatives and false positives at the authorization boundary — if wrong, clinician review may require narrowing individual versioned rules after the private pilot.

Final review: Ruling: every finite workflow gets an explicit cancel/open-conversation escape before value capture, one central abandoned-state cleanup, and reminder cancellation on every exit; later requests start from clean state — accidental free text must never become a contact or city — if wrong, a user who intended a literal value resembling a cancellation phrase will be asked to confirm it again.

Final review: Ruling: serialize and deduplicate every incoming update and make workflow effect, state, and audit persistence recoverable as one unit; mutually exclusive callbacks share an origin/workflow claim key and failures return truthful safe copy — external side effects may not be duplicated by Telegram retries or races — if wrong, the implementation adds database coordination overhead to a low-volume MVP.

Final review: Ruling: `/delete` must irreversibly anonymize or remove linked conversational/identity/provider data according to one explicit minimal audit-retention rule, and its confirmation must not recreate the deleted conversation; configured retention is enforced on both writes and reads and purge failures retry — documented privacy behavior must match executable behavior — if wrong, a stricter legal retention obligation must later extend the minimal non-identifying audit record.

Final review: Ruling: real PostgreSQL assurance, deterministic offline evaluation, smoke tests, and artifact checks are pre-restart deployment gates with rollback; no production restart is allowed while the real-PG gate is blocked — local green tests cannot validate PostgreSQL migration semantics — if wrong, deployment remains manual longer than necessary.

Final review: Ruling: address the four Minor findings in the same round because they are bounded and reinforce the same acceptance, UI, schema, and evidence contracts — leaving known integrity drift immediately before release has no meaningful MVP benefit — if wrong, the fix round is modestly larger.

Final review fix round 1/5: implementation `01e8607`, evidence/report `a17b779`; local Ruff and 332 tests, smoke, and 57-case offline evaluator green before scoped re-review.

Final review fix round 3/5: RED captured for 18 initial focused regressions,
durable tombstone/outbox ordering, worker revalidation, sequential evaluator
lifecycle, and a self-review stale-tombstone delivery finding. Ruling: retain
only a keyed identity hash plus next generation in a tombstone; this prevents
stale delivery/rebinding without retaining raw identity or content. Ruling:
the staged command path is fixed in production, with a non-secret local-stub
override solely for shell-flow tests. Ruling: provider-health diagnostic replay
keeps soft cases independently seeded because unavailable diagnostics cannot
create an offer; offline acceptance executes the required sequential lifecycle.
Fresh local acceptance: `just check` 390 passed, smoke PASS, evaluator 63
cases hard/diagnostic/provider/soft 0, shell syntax PASS, diff check PASS.

Final scoped re-review round 1: FAIL with 2 Critical, 7 Important, and 2 Minor residual findings. Critical: Telegram delivery still awaits outbound persistence before `message.answer`, and direct `не хочу жить` followed by ordinary distress text still misses crisis routing. Important: local-critical turns violate the agreed two-call contract; workflow refusals/processing reminders are incomplete; draft guard is literal rather than grammatical; text/start effects lack durable replay/serialization; stale outbound can recreate identity after `/delete`; legacy NULL retention and worker iteration are unsafe; staged release environment/rollback/assurance remain incomplete. Minor: URL PII accounting and evaluator soft-offer lifecycle.

Final re-review: Ruling: preserve exactly two concurrent diagnostics for every successfully prepared text turn, including locally critical turns, but make local critical copy independent of their result; fully prepare both inputs before creating either task so preparation failure starts zero calls and still returns canonical crisis copy — this honors the explicit product contract without allowing diagnostics to delay or authorize safety behavior — if wrong, crisis turns incur unnecessary provider cost and latency that can later be moved to an audited background outbox.

Final re-review: Ruling: user-visible delivery must be attempted before or independently of best-effort outbound audit persistence, and stale turns/jobs must carry a conversation generation rather than recreate deleted identity — availability of crisis copy and deletion finality outrank completeness of assistant-message logging — if wrong, some delivered messages may lack a local outbound audit during a database incident.

Final re-review: Ruling: direct completed existential clauses remain crisis even when followed by ordinary distress continuations, while only bounded locative/relationship complements suppress the rule — an allow-list of help-seeking tails is not a clause-aware safety boundary — if wrong, ambiguous figurative continuations may conservatively escalate to the simulated human path.

Final re-review: Ruling: stable update-derived effect keys, durable replayable outcomes, database conditional conversation transitions, and fault-injection tests are required before claiming idempotency; process-local locks are only an optimization — Telegram retries and multiple workers are normal runtime conditions — if wrong, the MVP carries additional outbox/version state before traffic justifies multiple workers.

Final re-review: Ruling: parse systemd EnvironmentFile without shell evaluation and expose only the database setting to staged assurance; protect every activation step with an ERR rollback trap and test the actual shell control flow with stubs — deployment must neither execute secret text as code nor strand the service between releases — if wrong, the stricter parser may reject an EnvironmentFile value that systemd itself accepts and require a documented encoding convention.

Final re-review: Ruling: fix both Minor residuals in round 2 because PII audit accuracy and executable soft-offer lifecycle evidence are part of the privacy/evaluator contracts, not cosmetic polish — if wrong, the round grows slightly without changing hard behavior.

Final review fix round 2/5: RED→GREEN scope complete. Canonical reply delivery is independent from outbound audit persistence; the extended clause-aware suicide rule, exactly-two diagnostic contract, refusal/processing-reminder guards, grammatical draft guard, durable outcome/effect replay, deletion generation binding, NULL-retention migration, worker iteration guard, non-evaluating staged gate, single-pass URL PII audit, and soft-offer lifecycle evaluation are covered offline. Ruling: persist only rendered text and structured UI choices in the claim outcome, then acknowledge after assistant-message persistence; acknowledged replay is suppressed, while an unacknowledged outcome is safely replayed without rerunning diagnostics. Ruling: EnvironmentFile parsing permits exactly one unquoted database URL and requires percent-encoding for values otherwise needing shell quotes. Fresh local evidence: Ruff clean, 363 tests, scenario smoke, and 60-case evaluator all green; evaluator hard/delta/provider/soft counts are zero. No live provider, Telegram, Podman, PostgreSQL, deployment, merge, or push occurred.

Final scoped re-review round 2: FAIL with 1 Critical, 7 Important, and 1 Minor residual. Critical: punctuation/prepositional continuations after a completed `не хочу жить` clause are still mistaken for locative/relationship complements. Important: refusal/finish cleanup remains literal/partial; definite future/passive action promises escape the draft guard; CAS/effect/outcome/delivery acknowledgement are not transactionally ordered; stale turns/jobs may send after deletion; NULL expiry and processing-job reclaim are incomplete; deploy exposes production DB credentials/PATH too broadly; PostgreSQL assurance does not prove index/runtime semantics. Minor: evaluator soft lifecycle is three seeded independent cases rather than a sequential replay.

Final re-review round 2: Ruling: clause boundaries must be retained as first-class matcher input; only same-clause bounded complements may suppress direct suicidal intent, never a new punctuation-delimited clause beginning with `в` or `с` — token-only tail inspection cannot satisfy the safety contract — if wrong, punctuation-free ambiguous messages may conservatively escalate.

Final re-review round 2: Ruling: acquire durable conversation/update serialization before deriving or applying any effect; use one immutable update identity for all request/escalation/action/outcome records, persist a delivery outbox, and acknowledge user delivery independently from optional audit persistence — fixing keys after effects are committed cannot provide exactly-once behavior — if wrong, a simpler single-process MVP could have tolerated occasional duplicate effects, but the claimed contract would be false.

Final re-review round 2: Ruling: deletion/cancellation establishes a durable tombstone/generation barrier and delivery must obtain authorization before Telegram send; crisis delivery may fail open only when the authorization store itself is unavailable, never after a confirmed tombstone — this balances deletion finality with emergency availability — if wrong, a rare DB outage may deliver a crisis message after a concurrent deletion request.

Final re-review round 2: Ruling: production database credentials are injected only into PostgreSQL assurance via a root-readable temporary environment file or stdin-safe equivalent, while sync/tests/evaluator run with a fixed root-owned PATH and synthetic offline DB URL — staged code must not receive broader production authority than its gate needs — if wrong, a dependency/tool requiring the user's Homebrew PATH must be given one explicit root-owned binary path.

Final re-review round 2: Ruling: PostgreSQL assurance must validate normalized table/ordered-column/predicate definitions and call the production repository semantics inside rollback; name-only indexes and hand-written surrogate SQL do not prove runtime compatibility — if wrong, assurance becomes more coupled to repository interfaces and needs versioned updates with schema changes.

Final re-review round 2: Ruling: finish the remaining Minor with a true sequential evaluator conversation because seeded end states cannot prove create/consume/expire transitions — executable lifecycle evidence is required for the accepted soft model suggestion exception — if wrong, the evaluator implementation grows beyond one-turn replay for a non-authoritative state.

Final review fix round 3/5: implementation `5da5c7d`; local Ruff and 390 tests, smoke, and 63-case offline evaluator green before scoped re-review.

Final scoped re-review round 3: FAIL with 2 Critical, 7 Important, and 1 Minor residual. Critical: matcher still discards punctuation and suppresses direct intent when a new clause starts with `в`/`с`; production authorization-store failure suppresses canonical crisis and replayed outcome loses critical classification. Important: refusal morphology/follow-up callbacks remain partial; draft future morphology remains partial; serialization locks stale detached state and effects commit before CAS; no dispatcher recovers failed Telegram sends; legacy processing rows/terminal denials remain unrecoverable; privileged deploy PATH remains environment-overridable; PG assurance still uses surrogate SQL. Minor: live evaluator bypasses sequential soft lifecycle and hard-codes history match.

Final re-review round 3: Ruling: escalate fix round 4 to a fresh strongest implementer per SDD rather than resume the prior agent — three rounds of local phrase/key fixes have not closed the underlying parser, transaction, outbox, and repository-seam defects — if wrong, the context reset costs time but avoids anchoring on the current implementation.

Final re-review round 3: Ruling: represent clauses explicitly (span start/end plus boundary kind) and increment matcher/dataset version; direct-suicide suppression is allowed only when the complement shares the intent clause — repeated token-tail patches are prohibited — if wrong, matcher and fixtures require one intentional version migration.

Final re-review round 3: Ruling: persist `critical_delivery` in the durable outcome and use tri-state authorization: confirmed tombstone/mismatch denies, unavailable storage fails open only for canonical critical output — crisis availability and deletion finality both need explicit evidence, not exception-order inference — if wrong, a database outage may release only a canonical crisis message whose audit cannot be persisted.

Final re-review round 3: Ruling: conversation serialization must encompass a fresh state read and all effect/state/outcome writes through one transaction/session seam; no external-effect commit may precede a stale-state check — locking a detached snapshot cannot prevent cross-process duplicate aid — if wrong, repositories require a larger unit-of-work refactor than MVP traffic alone would justify.

Final re-review round 3: Ruling: a durable outcome is not an outbox without an independent reclaimer/dispatcher; failed sends must be retried without replaying diagnostics and durably acknowledged idempotently — Telegram will fail independently of inbound redelivery — if wrong, the MVP gains a delivery worker earlier than needed.

Final re-review round 3: Ruling: privileged deploy PATH is a constant inside the root script and tests inject stubs through an explicitly non-privileged harness, never an environment override consumed by production sudo — root execution may not select user-writable binaries — if wrong, VM-specific Homebrew locations need explicit absolute configuration owned by root.

Final review fix round 4/5: implementation `8c4855e`; local Ruff and 419 tests, smoke, and 65-case offline evaluator green before scoped re-review.

Final scoped re-review round 4: FAIL with 1 Critical and 4 Important residuals, no Minor. Critical: an unpersisted canonical crisis turn sets `skip_outbound_persistence` and bypasses confirmed tombstone authorization. Important: successful Telegram send followed by acknowledgement failure is inherently replayable; two common refusal verb families remain uncovered; draft-guard predicate/polarity/conditional scope is unsound; deploy ownership verification rejects legitimate root-owned symlink layouts while the harness disables verification.

Final breaker round 5: Ruling: every critical turn, including unpersisted fallback, enters tri-state identity/tombstone authorization held through send; `skip_outbound_persistence` suppresses audit only and never authorization — confirmed deletion must deny while unavailable storage may fail open — if wrong, an unpersisted critical turn may need a temporary synthetic identity binding to distinguish absence from confirmed tombstone.

Final breaker round 5: Ruling: Telegram Bot API does not provide an idempotency key for message send, so exactly-once visible delivery across “send succeeded, acknowledgement commit failed” is not implementable; explicitly adopt bounded at-least-once delivery for that ambiguity, persist/reclaim outcomes, surface a delivery-ambiguity audit/metric, and remove every exactly-once claim — this is an accepted transport limitation, not hidden correctness — if wrong, a future Telegram/API proxy with idempotency support can restore exactly-once at the adapter boundary.

Final breaker round 5: Ruling: refusal parsing and draft-action guarding use clause-bound predicate families with morphology; broad-window marker searches and isolated literal phrases are prohibited — this closes the remaining reproduced word-order/conditional/noun false classifications — if wrong, conservative canonical copy may replace unusual benign drafts.

Final breaker round 5: Ruling: production tool verification resolves and validates every symlink hop plus the final target with fixed root-owned `readlink`/`stat`; permission checks apply to resolved targets/components, while harness exercises both a valid root-equivalent symlink chain and a writable-target rejection without disabling the verifier — if wrong, an uncommon VM symlink topology fails closed and requires an explicit verified path update.

Final breaker round 5: Ruling: after round 5, adjudicate every reviewer residual explicitly and do not defer any Critical; merge/deploy only if re-review has no load-bearing safety/privacy correctness finding and the real PostgreSQL release gate passes — the breaker cap prevents unbounded prompt-only loops — if wrong, a non-load-bearing limitation may be documented instead of delaying the private MVP.

Final review fix round 5/5: RED→GREEN implementation complete. Unpersisted critical turns retain identity/generation evidence and always enter tri-state tombstone authorization; confirmed deletion denies, while unavailable authorization fails open only for canonical critical copy. Delivery acknowledgement failure is persisted as the finite `delivery_ambiguous` category and reclaims the same outcome without rerunning diagnostics or business effects; the product explicitly documents bounded at-least-once visible delivery. Workflow refusal and draft-action decisions now use clause-bound predicate families, including the final reproduced verb/conditional/noun cases. Production tool verification has no runtime PATH override, walks and validates symlink ownership and resolved non-writable components, and its harness exercises verification instead of disabling it. Fresh local evidence at the uncommitted fix diff: Ruff clean, 442 tests, scenario smoke, 24 evaluator tests and 65 cases with zero hard/diagnostic/provider/soft failures, both deploy shell syntax checks, and `git diff --check` all pass. No provider, Telegram, Podman, PostgreSQL, deployment, merge, or push occurred.

Final breaker adjudication: final scoped review found one load-bearing Critical and no Important: the pending-outcome worker failed closed for `UNAVAILABLE` authorization even when the persisted outcome was canonical critical. Ruling: worker replay uses the same tri-state rule as direct delivery — confirmed denial always stops; unavailable storage fails open only for persisted `critical_delivery`; ordinary outcomes remain fail closed — divergent initial/replay crisis availability is not acceptable — if wrong, a transient authorization-store outage can release only the already persisted canonical crisis copy. The stale tombstone comment and one obsolete exactly-once ledger phrase are corrected in the same bounded patch.

Release live gate: the first final 65-case run had 0 provider failures but reported 6 hard failures downstream of one missing optional `suggested_support=psychologist` soft offer, plus the originating soft failure. Root-cause trace proved the evaluator applied the hard consume expectation although its diagnostic-owned precondition was never created. Ruling: when a live optional soft offer is absent, only its dependent choice/effect/state/rule/canonical-copy fields are downgraded to an explicit `pending_offer_precondition` soft quality failure; unrelated hard invariants remain blocking. Live soft failures are observable and non-blocking, while offline fixture soft failures still fail acceptance — a non-authoritative diagnostic suggestion cannot masquerade as deterministic product authority — if wrong, a separate approved minimum soft-offer quality threshold must be added before the external pilot.

Final release live gate on the soft-precondition evaluator diff: health check passed; sequential run 1 had 65 cases, 0 hard failures, 0 provider failures, 45 diagnostic deltas, and 3 soft failures; run 2 had 65 cases, 0 hard failures, 0 provider failures, 44 diagnostic deltas, and 3 soft failures. Ruling: accept the live hard/provider gate reproducibly and retain both diagnostic and soft drift as explicitly non-authoritative quality metrics for prompt/model review — deterministic product behavior stayed invariant across both runs — if wrong, stakeholders must define and approve a separate soft/diagnostic quality threshold before expanding beyond the private pilot.

Final review fix round 4/5: underlying architecture scope complete from base `5da5c7d`. Clause spans and punctuation are first-class matcher-v3 input; bounded state-aware refusal and draft grammars replace sentence lists. One identity-locked database unit of work now owns fresh state, immutable update claim, effect/state/action/outcome writes, with callbacks and text in distinct durable execution namespaces. Persisted critical classification plus tri-state pre-send authorization preserves canonical crisis fail-open only for unavailable storage while confirmed tombstones deny. An independent pending-outcome worker, NULL/expired delivery and follow-up reclaim, terminal job cancellation, fixed root-owned deploy executables, rollback-bound production repository assurance, and live/offline sequential evaluator parity are covered by synthetic RED→GREEN tests.

Final review round 4: Ruling: nested production repository writes flush a ContextVar-bound `AsyncSession` and only the outer UoW commits; standalone repository callers retain their commit behavior — this makes state/effect/outcome atomic without duplicating every repository API — if wrong, the session seam can be replaced by explicit session parameters without weakening the transaction tests.

Final review round 4: Ruling: durable inbound identity includes event kind because callback source-message ids and inbound text ids are not a safe shared namespace; deployed numeric text keys remain unchanged for replay compatibility — if wrong, the extra callback prefix is harmless storage metadata.

Final review round 4: Ruling: pending outcome delivery acknowledges before optional assistant audit and never reruns diagnostics; a failed adapter send releases or rolls back its lease for worker reclaim — recovery must not depend on Telegram redelivering an inbound update — if wrong, a provider-free outbox worker is modestly earlier infrastructure for the MVP.

Final review round 4: Ruling: PostgreSQL assurance may directly shape only legacy fixture columns, but every operation under test must be the actual production repository method on one rollback-bound session — surrogate CRUD cannot prove production semantics — if wrong, assurance remains more tightly coupled to intentional repository changes.

Final review fix round 4/5 fresh local acceptance: Ruff clean and 419 tests passed; scenario smoke PASS; version-3 evaluator 24 focused tests and 65 cases with hard/diagnostic/provider/soft counts all zero; both deploy scripts pass `bash -n`; diff check PASS. Transaction/commit boundaries and every privileged executable/path component were manually inspected. No live provider, Telegram, Podman, PostgreSQL, deployment, merge, or push occurred. Detailed evidence: `final-fix-round-4-report.md`.
