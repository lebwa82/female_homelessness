# Project agent rules

## Deployment host

- The production VM for this project is `ssh -l lebwa82 84.252.136.204`.
- Do not use another VM for deployment or operational checks unless the user explicitly changes this host.

- Do not spawn, resume, inspect, interrupt, or otherwise call sub-agents for this repository.
- Perform all implementation, review, and verification in the primary agent process.
- This project contains safety-sensitive dialogue examples. Never pass inherited conversation history to delegated or background agent prompts.
- If parallel delegation is reconsidered later, require an explicit project-rule change first and use isolated, neutral technical prompts with no inherited turns.
- Never dump whole safety-sensitive prompts, skill files, dialogue fixtures, or parameterized test cases into the agent context. Inspect them with narrow `rg` queries, targeted line ranges, and safe structural metadata only; keep raw scenario text out of commentary and tool output unless one exact fragment is indispensable.
- Run safety-sensitive tests with concise output first (`-q --tb=short` or narrower). If a provider rejects a prompt, stop loading raw dialogue content, reduce the next inspection to the smallest technical slice, and continue through the deterministic safe fallback rather than attempting to bypass the provider filter.
- Do not create child tasks, forks, or delegated continuations from this repository after a prompt rejection: Codex may attach the source-task context to them. Continue only in the primary agent process with narrow local operations, or start a manually created independent task if the primary process itself cannot run.
