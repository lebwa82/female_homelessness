# Project agent rules

## Delivery and verification

- Answer in the user's language, concisely; use feminine forms in Russian. Distinguish verified results, inference and remaining work. Report local checks, Git publication and live production verification separately.
- For implementation, finish the smallest in-scope change, relevant regression checks and final diff/status. Diagnosis or review alone does not authorize implementation. Preserve unrelated dirty and untracked work.
- Prefer maintained project tools and installed skills. Repair only prerequisites needed for the requested outcome; optional tooling or an unavailable knowledge base must not block independent work.
- Retain background operation IDs, await completion and inspect results. Read back uncertain external writes before retrying.

## Local workflow and skills

- This checkout is a standalone Python project on macOS. Use scoped `ya tool ast-index` for unfamiliar symbols when available, then scoped `rg` for literals, configuration or an empty symbol lookup. Inspect an outline before reading a slice of a source file over 500 lines. Arcadia VCS/build instructions apply only in an actual Arcadia checkout.
- Run project Python commands through `uv run`. Choose deterministic tests that cover changed behavior and business invariants; use `-q --tb=short` for safety-sensitive tests. Check available targets in `justfile`, keeping its legacy deployment targets subject to the Timeweb rules below.
- For Chatwoot acceptance, read `~/.codex/skills/chatwoot-bot-testing/SKILL.md`; for deployment, read `docs/deploy-timeweb-from-mac.md`; for runtime operations, read `docs/chatwoot-operations.md`. Read only references relevant to the requested work.
- Skills imported from Betelgeuse are user-level tools. Follow this project's execution and safety boundaries when a skill has broader instructions. Installing a skill does not authorize its hooks, telemetry, daemon, automatic memory writes or external effects; preserve existing opt-ins and disabled skills.
- Betelgeuse (`betelgeuse.klg.yp-c.yandex.net`) is a development VM and the skill source. It is separate from the Timeweb production target below. Use existing SSH access for requested VM files; local `/Users/lebwa82/...` paths remain on the Mac.
- Protect secrets through maintained helpers and read `~/.stefania/agent-core/references/safety.md` before credential handling. Keep local MCP, authentication and integration settings; transfer skill packages without credentials. Verify agent or MCP configuration changes in a fresh process before claiming they work.
- For notes or past decisions, read `~/.codex/instructions/notes.md`. Self-contained code or configuration edits do not require unrelated history research. Personal memory writes require an explicit user request.

## Git delivery

- Standing user authorization: after completing requested project changes and relevant checks, commit the in-scope changes and push them to `origin/main` without asking again. Preserve unrelated unfinished work, integrate remote updates non-destructively, and verify that the remote branch contains the delivered commits. Never force-push.
- This authorization covers Git publication, not deployment or other production mutations; those retain their separate approval boundaries.

## Deployment host — Timeweb (confirmed by the user, 2026-10-20)

- The active contour is Timeweb, not Yandex Cloud. VM `9351701` / `nevidimy-prod-app`, managed PostgreSQL `4221087` / `nevidimy-prod-pg`, project `3044129`, region `msk-1`. See `docs/chatwoot-operations.md`, section "Timeweb: новый контур".
- Before remote operations, refresh and verify the VM's ID, name, project and current IP through the Timeweb API. Use the previously trusted SSH host key; do not guess an IP from old messages. Before mutations, also check `/etc/nevidimy-deployment-target` equals `timeweb-nevidimy-prod-app-msk-1`. The marker alone is not independent proof of identity. Missing API/SSH access blocks remote changes.
- Runtime is Docker Compose in `/opt/nevidimy/app`. Core: `nevidimy.service`; backend: `nevidimy-agent.service`; Telegram: `nevidimy-telegram.service` and `nevidimy-proxy-tunnel.service`.
- Compose order is `deploy/chatwoot/compose.yml`, `deploy/chatwoot/compose.managed.yml`, `deploy/timeweb/compose.yml`, followed by the explicitly active overlays. Use `scripts/chatwoot_compose.py` for both systemd and manual operations. On 2026-10-20 the live UI used `compose.domain-https.yml` and `public-domain-https.conf`, taking precedence over the older loopback drop-in. Preserve the existing public HTTPS hostname/certificate; inspect actual units/drop-ins before activation.
- Managed PostgreSQL already requires `verify-full` and the provider CA at `/opt/nevidimy/certs/postgresql-ca.crt`. Never start local PostgreSQL or use the internal application CA as the managed database CA. Preserve pinned images, volumes, existing provider selection and credentials.
- Preserve the actually configured Telegram identity, credentials, cursor and imported history during updates. The user previously authorized production `@nevidima_bot`; neither switch back to MVP nor repeat the cutover/import automatically. LLM is Timeweb AI Gateway / DeepSeek; legacy Yandex defaults are not the active provider.
- `just prod-host`, `scripts.resolve_prod_host`, `just deploy-prod`, `scripts/deploy_chatwoot_test.sh`, `activate.py` and `women-help-*.service` are legacy Yandex/Podman tooling. Do not use them for active production operations. Do not restart `women-help-bot.service`.
- Do not use another VM unless the user explicitly changes the project target. Current runtime access is separate from these project records; never claim that recorded deployment state proves live connectivity.
- The user cancelled cloud-access integration on 2026-10-20. Deploy these changes from the user's Mac using its existing authorized API/SSH access; follow `docs/deploy-timeweb-from-mac.md`. Do not make cloud secret bindings, a new VM, or a VPN a prerequisite. Old access-handoff notes are task-local artifacts, not deployment instructions.

## Agent execution and sensitive material

- Default: perform implementation, review, and verification in the primary agent process; do not use sub-agents.
- User-approved exception (2026-10-03): one isolated Chatwoot testing agent may prepare the test plan and run local, non-network tests. Start it with no inherited turns and a neutral technical brief. It may edit only its test-plan document; no credentials, customer data, runtime edits, remote mutations, deployment, or further delegation. The primary agent configures the CLI and performs authorized live tests in isolated test conversations. This exception does not authorize other delegated work.
- Additional one-off user-approved exception (2026-10-03): one isolated diagnostic agent may inspect Chatwoot operational metadata using the configured official CLI. No inherited turns, raw conversation content, credential disclosure, code/config edits, remote mutations, customer messages, or further delegation. Report evidence only; the primary agent performs any separately authorized repair.
- This project contains safety-sensitive dialogue examples. Never pass inherited conversation history to delegated or background agent prompts.
- If parallel delegation is reconsidered later, require an explicit project-rule change first and use isolated, neutral technical prompts with no inherited turns.
- Never dump whole safety-sensitive prompts, skill files, dialogue fixtures, or parameterized test cases into the agent context. Inspect them with narrow `rg` queries, targeted line ranges, and safe structural metadata only; keep raw scenario text out of commentary and tool output unless one exact fragment is indispensable.
- Run safety-sensitive tests with concise output first (`-q --tb=short` or narrower). If a provider rejects a prompt, stop loading raw dialogue content, reduce the next inspection to the smallest technical slice, and continue through the deterministic safe fallback rather than attempting to bypass the provider filter.
- Do not create child tasks, forks, or delegated continuations from this repository after a prompt rejection: Codex may attach the source-task context to them. Continue only in the primary agent process with narrow local operations, or start a manually created independent task if the primary process itself cannot run.
