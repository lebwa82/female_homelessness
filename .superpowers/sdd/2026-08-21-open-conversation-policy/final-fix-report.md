# Final security and correctness fix report

Implementation commit: `01e8607` (`Harden conversation policy boundaries`).

## Scope and process

This round addressed every Critical, Important, and Minor finding in the final
fix brief. Work remained in the designated feature worktree. No live provider,
Telegram, deployment, Podman, or real PostgreSQL call was made. All diagnostic
fixtures and regression assertions use safe synthetic data; this report contains
no contact value, history, secret, or provider-controlled prose.

Strict TDD evidence was captured before each production change:

- The initial privacy/safety regression module was RED with 36 failures (and 6
  already-passing controls) before the privacy, direct-crisis, negation,
  workflow, and safety-order fixes.
- The initial durable regression module was RED before the missing durable
  behavior was implemented. Its parser, deletion, retention, audit, evaluator,
  and release-gate checks then turned GREEN.
- The independent final read-only review identified five additional gaps. Their
  focused RED set failed in all five categories: critical copy after PII/write
  failure, recoverable text claim, duplicate start update, runtime PSL usage,
  and legacy escalation constraint cleanup.
- A final migration regression for a surviving standalone legacy unique index
  was RED before adding its explicit cleanup.
- During final verification, two pre-existing callback-retry tests caught an
  overly broad service-layer exception conversion. The boundary fallback was
  moved to the Telegram adapter, preserving the service retry contract; the
  original tests and the final full suite are GREEN.

## Implemented architecture

- PII/model boundary: local redaction recognizes Telegram handles; typed
  workflow contacts always become exactly `[CONTACT]` in current and historical
  model views. Runtime URL masking uses a `tldextract` instance configured with
  no public-suffix refresh. Provider audit persistence is a finite allow-list
  with mapped validation categories only.
- Safety/policy: local signals own authorization. The new direct-crisis rules,
  clause-scoped negation, explicit-human grammar, transactional negation,
  external-action draft guard, and workflow-cancel effect are deterministic and
  corpus-versioned. Critical copy is returned even if local message redaction or
  persistence fails, and critical turns never start diagnostics.
- Durable state: inbound text claims have lease, completion, and failure states
  so a partial turn can be retried. Start, stateless command/media, callbacks,
  and text updates have deduplication boundaries; same-keyboard callbacks are
  mutually exclusive. The Telegram boundary supplies a truthful retry/human
  fallback while callback lease recovery remains intact.
- Data lifecycle: configured retention controls message/contact expiry and read
  filtering; the worker survives transient purge errors. `/delete` removes all
  linked rows in one transaction without persisting a replacement identity.
- Schema/deployment/evaluation: the escalation request key now has one named
  unique structure after legacy constraint/index cleanup; staged deployment runs
  offline checks and target PostgreSQL assurance before activation. The evaluator
  rejects duplicate/non-standard JSON, reports pending offers as soft state, and
  includes the two-turn support-offer lifecycle.
- Documentation: the obsolete design is marked superseded and the README now
  states the diagnostic-only model boundary, PII/deletion/retention behavior,
  and staged release gate accurately.

## Fresh verification for `01e8607`

| Command | Result |
| --- | --- |
| `just check` | PASS — Ruff clean; 332 tests passed |
| `just scenario-smoke` | PASS |
| `just eval-dialogues` | PASS — 22 evaluator tests; 57 fixture cases; hard failures 0, diagnostic deltas 0, provider failures 0 |
| `bash -n scripts/deploy_prod.sh` | PASS |
| `git diff --check` | PASS before commit |
| focused review/regression set | PASS — 67 tests |

## Residual concern

The production PostgreSQL assurance and staged deployment were intentionally not
executed in this task because the contract forbids real database, Podman, and
deployment interaction. The committed gate fails closed when its root-only
environment, healthy existing PostgreSQL container, or assurance step is
unavailable; its ordering and schema behavior are covered offline.
