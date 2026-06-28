# Agent-Driven Trello → Code Pipeline: Implementation Plan

This plan is structured for a Claude Code session with subagent delegation. Phases are ordered by dependency; within each phase, parallelizable work is called out explicitly with subagent prompts at the bottom.

## Pre-flight (do these once before kicking off)

- [ ] Trello API key + token in hand. Generated via Power-Up at https://trello.com/power-ups/admin.
- [ ] Test board created with the six lists named **exactly**: `Backlog`, `Todo`, `InProgress`, `Review`, `Merge`, `Done`. Mismatched names will fail startup validation.
- [ ] GitHub repo for the target project exists, with `gh` CLI authenticated (`gh auth status`).
- [ ] Public HTTPS endpoint for webhooks. ngrok or Cloudflare Tunnel is fine for dev. Trello won't talk to localhost.
- [ ] Anthropic API key for the agent. Confirm tier headroom for ~12 concurrent multi-turn sessions.
- [ ] Decision: where does the orchestrator run? Local machine for v0 is fine; revisit when you want it always-on.

## Tech stack

Locked in based on prior conversation:

- **Python 3.12+**, `uv` for env management
- `httpx` (async) + `pydantic` v2 for the Trello client
- FastAPI + uvicorn for the webhook receiver
- SQLite (WAL mode) via stdlib `sqlite3` — no ORM
- `apscheduler` for the polling fallback
- `gh` CLI shelled via `subprocess` for GitHub ops
- `git worktree` shelled via `subprocess`
- Claude Code subprocess (`claude --print`) for the agent
- `pytest` + `pytest-asyncio` for tests
- `ruff` for lint/format

## Repo layout

```
agents-trello/
  pyproject.toml
  .env.example
  README.md
  src/agents_trello/
    __init__.py
    config.py                # env-driven config, frozen dataclass
    app.py                   # composition root: builds provider, deps, FastAPI app

    domain/
      __init__.py
      models.py              # Task, Comment, Column, TaskId
      events.py              # DomainEvent union types
      provider.py            # BoardProvider protocol
      handlers/
        __init__.py          # dispatcher: DomainEvent -> handler
        on_task_moved.py     # column-routing handlers
        on_comment_added.py  # review-loop logic
        ports.py             # protocols for Worktree, Agent, VCS — handler deps

    adapters/
      __init__.py
      trello/
        __init__.py
        client.py            # raw httpx wrapper, typed pydantic responses
        provider.py          # implements BoardProvider
        webhook_route.py     # FastAPI router; verifies signature, parses, dispatches
        signature.py         # HMAC-SHA1 verification
        mapping.py           # list-name <-> Column, action-type -> DomainEvent
      inmemory/
        provider.py          # for tests + future scaffolding
      github/
        client.py            # gh CLI wrappers (open_pr, mark_ready, enable_automerge, …)
      worktree/
        manager.py           # create_worktree, remove_worktree, push_branch
      agent/
        runner.py            # spawn Claude Code in a worktree, capture output
        prompts.py           # system prompt templates (initial work, review loop)

    infra/
      db.py                  # schema, migrations, connection helpers
      locks.py               # per-task SQLite-backed lock
      logging.py             # structlog setup

    backfill.py              # startup polling: fetch missed actions since cursor
    cli.py                   # typer/argparse: register-webhook, list-cards, replay

  tests/
    conftest.py
    domain/
      test_handlers_on_move.py
      test_handlers_on_comment.py
    adapters/
      trello/
        test_mapping.py
        test_signature.py
        test_provider_parses_webhook.py
    integration/
      test_end_to_end_inmemory.py
```

The acid test for the abstraction: deleting `adapters/trello/` should cause type errors only in `app.py` and the webhook route file. Nothing in `domain/` should import from `adapters/`.

---

## Phase 0 — Scaffolding (single agent, ~30 min)

**Goal:** boring boilerplate done in one shot so Phase 1 can focus on design.

**Deliverables:**
- `pyproject.toml` with deps and `ruff`/`pytest` config
- `.env.example` with all env vars documented
- Empty package skeleton matching the layout above (each `__init__.py`, empty modules with docstrings)
- `infra/logging.py` with structlog wired
- `Makefile` or `justfile` with `install`, `lint`, `test`, `run` targets
- README with quickstart

**Acceptance:** `make install && make lint && make test` runs (test suite empty, but pytest exits 0).

**Subagent suitability:** single agent, no parallelization needed.

---

## Phase 1 — Domain core (single agent, ~1–2 hr) — **GATE**

**This phase blocks everything else.** Get it right; everything downstream consumes these types.

**Goal:** define the domain model, event types, provider protocol, and handler shape. Prove the abstraction with a working `InMemoryProvider` and handler tests that have zero adapter dependencies.

**Deliverables:**

1. `domain/models.py`
   - `Column` enum with the 6 values
   - `TaskId`, `Task`, `Comment` dataclasses (frozen where sensible)
2. `domain/events.py`
   - `TaskCreated`, `TaskMoved`, `CommentAdded`, `TaskUpdated`
   - `DomainEvent = Union[...]`
3. `domain/provider.py`
   - `BoardProvider` Protocol with: `list_tasks`, `get_task`, `get_comments(since)`, `post_comment`, `move_task`, `update_description`, `poll_events(since_cursor) -> (events, new_cursor)`, `parse_webhook(headers, body) -> list[DomainEvent]`
4. `domain/handlers/ports.py`
   - `WorktreeManager`, `AgentRunner`, `VCS` protocols — handler dependencies as ports
5. `domain/handlers/__init__.py`
   - `Dispatcher` class with `register(event_type, handler)` and `dispatch(event)` — async, awaits handlers
6. `adapters/inmemory/provider.py`
   - Full implementation of `BoardProvider` backed by dicts
   - Helper methods for tests: `seed_task(...)`, `simulate_move(...)`, `simulate_comment(...)` — these emit events into a queue that test code can drain
7. `tests/domain/test_handlers_on_move.py`
   - Test: Todo→InProgress triggers worktree creation + agent spawn (verify via fake `WorktreeManager`/`AgentRunner` ports)
   - Test: Review→Merge marks PR ready and enables auto-merge (verify via fake `VCS`)
   - Test: any→Backlog cancels in-flight agent
   - Test: unknown column transition is a no-op + logs warning
8. `tests/domain/test_handlers_on_comment.py`
   - Test: comment on a Review card by a human triggers agent invocation with thread context
   - Test: comment by the agent itself is ignored (no infinite loop)
   - Test: comment on non-Review card is ignored

**Acceptance:**
- All Phase 1 tests pass
- `mypy --strict src/agents_trello/domain/` clean (or `pyright` strict)
- Grep check: `grep -r "from.*adapters" src/agents_trello/domain/` returns nothing

**Why this is the gate:** if the handler tests can be written and pass against `InMemoryProvider` + fake ports, the abstraction is real. If you find yourself reaching for Trello concepts in a handler, stop and fix the domain.

**Subagent suitability:** single agent, this is design work that needs one consistent voice.

---

## Phase 2 — Parallel buildout (4 subagents, ~2–3 hr each)

After Phase 1, four streams are independent. Each subagent works in its own git worktree off `main`. Each owns a single top-level package and its tests.

### 2A — Persistence + dispatcher wiring

**Owner subagent prompt:** see "Subagent prompts" section below.

**Deliverables:**
- `infra/db.py`: SQLite schema with tables `cards` (task_id PK, short_id, current_column, branch_name, worktree_path, agent_status, last_review_comment_id, updated_at), `processed_actions` (action_id PK, processed_at), `webhook_cursor` (singleton row), `event_log` (id, event_json, processed_at, error). WAL mode on connect. Migration runner.
- `infra/locks.py`: per-task lock backed by `BEGIN IMMEDIATE` on a `card_locks` table — context manager API: `async with task_lock(task_id):`
- Idempotency middleware in dispatcher: skip events whose `action_id` is already in `processed_actions`.
- Tests: schema migration round-trip, lock prevents concurrent same-task work, dispatcher idempotency.

**Acceptance:** dispatcher can be hammered with duplicate events at concurrency 12 across 5 task IDs and produces correct serialized handler invocations per task.

### 2B — Trello adapter

**Deliverables:**
- `adapters/trello/client.py`: typed httpx client with methods for boards, lists, cards, comments, actions, webhooks. All methods async. Pydantic models for responses.
- `adapters/trello/mapping.py`: `list_name_to_column`, `action_to_events` — pure functions, heavily tested.
- `adapters/trello/provider.py`: implements `BoardProvider`. Resolves list IDs at startup and fails loud if any column is missing.
- `adapters/trello/signature.py`: HMAC-SHA1 verification per Trello's docs.
- `adapters/trello/webhook_route.py`: FastAPI router, handles `HEAD` (must 200 for registration) and `POST`.
- `cli.py` command: `register-webhook --board-id ... --callback-url ...`
- Tests: mapping pure-function tests against captured Trello payloads, signature verify with known-good vector, provider integration tests against a recorded session (use `respx` to mock httpx).

**Acceptance:** given a fixture of real Trello action payloads (capture a few from your test board), `parse_webhook` produces the expected `DomainEvent` list.

### 2C — Worktree + agent runner

**Deliverables:**
- `adapters/worktree/manager.py`: implements `WorktreeManager` port. `create(task_id, slug, base="main") -> Path`, `remove(task_id)`, `push(task_id, remote="origin")`. All operations subprocess `git worktree`.
- `adapters/agent/prompts.py`: two prompt templates — initial work (column → InProgress) and review loop (new comments on Review). Templates explicitly require: write `docs/feat/<slug>.md`, commit it, push branch.
- `adapters/agent/runner.py`: implements `AgentRunner` port. Spawns `claude --print` in the worktree with the prompt on stdin. Captures stdout/stderr. Returns structured result: `{success: bool, summary: str, files_changed: list[str], error: str | None}`.
- Tests: worktree create/remove with a temp git repo fixture, agent runner with a fake `claude` shim that echoes the prompt back.

**Acceptance:** end-to-end test creates a worktree from a fixture repo, "agent" makes a fake change, manager pushes the branch, manager removes the worktree.

### 2D — GitHub adapter

**Deliverables:**
- `adapters/github/client.py`: implements `VCS` port. Methods: `open_draft_pr(branch, title, body) -> pr_url`, `mark_ready_for_review(pr_url)`, `enable_auto_merge(pr_url)`, `get_pr_status(pr_url) -> {state, checks_passed, mergeable}`.
- All methods shell out to `gh` CLI; parse JSON output (`gh pr view --json ...`).
- Webhook receiver route for GitHub Actions: `POST /github/webhook` accepting `{event: "deployment_ready", pr_url, deploy_url}` and `{event: "merged", pr_url}`. Verify GitHub HMAC signature.
- GitHub Actions workflow templates (in `templates/`) that the user installs in the target repo: one for deploy-preview-on-feat-branch posting back the URL, one for merge-to-main posting back the merge event.

**Acceptance:** unit tests with `gh` mocked via `subprocess.run` patching. Integration test against a throwaway repo (gated behind `INTEGRATION=1` env var).

---

## Phase 3 — Composition + webhook receiver (single agent, ~1–2 hr)

**Goal:** wire everything together. This is `app.py` and the FastAPI startup.

**Deliverables:**
- `app.py`:
  - Build config from env
  - Construct `TrelloProvider`, `GitHubVCS`, `WorktreeManagerImpl`, `ClaudeAgentRunner`
  - Construct `Dispatcher`; register all handlers with their dependencies injected
  - Build FastAPI app, mount `trello_webhook_route` and `github_webhook_route`
  - Startup: run `backfill.run()` to catch missed actions, then start scheduler for periodic polling fallback (every 5 min as safety net)
- `backfill.py`: on boot, read `webhook_cursor`, call `provider.poll_events(since=cursor)`, push events through dispatcher, update cursor.
- `cli.py`: `register-webhook`, `list-cards`, `replay-event <action_id>`, `dry-run-handler <event_json>`.

**Acceptance:**
- `uvicorn agents_trello.app:app` starts, registers webhook on first run
- Manual test: move a card on the test board → handler fires → log line confirms

---

## Phase 4 — End-to-end on the test board (single agent, ~half day with the actual board)

**Goal:** drive a real card through the full state machine on a real Trello board with a throwaway repo.

**Test script:**
1. Create a card in `Backlog` with a trivial spec (e.g., "Add a `/health` endpoint that returns `{ok: true}`").
2. Move to `Todo`. Verify: noop, log line.
3. Move to `InProgress`. Verify: worktree created, agent spawned, branch pushed, `docs/feat/...md` exists, draft PR opened, deploy URL eventually posted as comment.
4. Add a human comment: "please also add a unit test." Verify: agent run triggered, follow-up commit, comment posted summarizing what changed.
5. Move to `Merge`. Verify: PR marked ready, auto-merge enabled.
6. Wait for CI green. Verify: GitHub webhook fires, card moves to `Done`, worktree cleaned up, remote branch deleted.

**Acceptance:** the above runs without manual intervention beyond the two human-driven steps (initial move and comment).

---

## Subagent prompts (copy-paste ready)

Each prompt assumes the subagent is launched in a worktree off `main`, has access to `IMPLEMENTATION_PLAN.md`, and the Phase 1 work is merged before Phase 2 subagents start.

### 2A — Persistence + dispatcher

```
You own the persistence and event dispatch layer for an agent-driven Trello pipeline.
Read IMPLEMENTATION_PLAN.md sections "Phase 1" and "Phase 2A" before starting.

Deliverables: infra/db.py, infra/locks.py, dispatcher idempotency middleware in
domain/handlers/__init__.py.

Constraints:
- SQLite WAL mode. No ORM.
- Per-task locking via BEGIN IMMEDIATE on a card_locks table; context-manager API.
- Idempotency keyed on action_id from DomainEvent metadata (add an action_id field to
  the event base if it isn't there yet — coordinate with the domain owner if so).
- All async. Use aiosqlite or run sync sqlite in a thread executor.

Tests required:
- Migration round-trip
- Per-task lock serializes same-task, parallelizes different-task
- Dispatcher skips duplicate action_ids
- Hammer test: 100 events, 5 task IDs, concurrency 12 — assert per-task ordering preserved

Done when: pytest -k "infra or dispatcher" green, mypy clean on infra/.
```

### 2B — Trello adapter

```
You own the Trello adapter for an agent-driven pipeline. Read IMPLEMENTATION_PLAN.md
sections "Phase 1" and "Phase 2B" before starting. The domain types and BoardProvider
protocol are already defined in src/agents_trello/domain/ — do not modify them.

Deliverables: adapters/trello/{client,provider,signature,webhook_route,mapping}.py
plus a CLI command to register a webhook.

Constraints:
- All Trello-specific types stay inside adapters/trello/. The provider returns only
  domain types.
- Resolve list-name -> list-id at startup; fail loud if any of the 6 expected columns
  is missing on the board.
- Webhook route: HEAD must return 200 for Trello registration. POST verifies
  HMAC-SHA1 signature in x-trello-webhook header before parsing.
- Capture 5-10 real Trello action payloads from a test board into tests/fixtures/
  and use them in mapping tests.

Tests required:
- mapping.action_to_events() against fixture payloads
- signature verify with known-good and known-bad vectors
- provider.parse_webhook end-to-end with a fixture POST body
- provider.poll_events handles cursor pagination correctly

Done when: pytest -k trello green, fixture-based tests cover updateCard:idList,
commentCard, createCard, updateCard (rename/desc), deleteCard.
```

### 2C — Worktree + agent runner

```
You own the worktree management and Claude Code agent runner. Read
IMPLEMENTATION_PLAN.md sections "Phase 1" and "Phase 2C" before starting.

Deliverables: adapters/worktree/manager.py, adapters/agent/{runner,prompts}.py.

Constraints:
- Worktrees live at ~/agents/worktrees/<short_id>-<slug>/. Configurable via env.
- Branch naming: feat/<short_id>-<slug>. Slug from card title, lowercased,
  alphanum + dashes only, max 40 chars.
- Agent runner spawns `claude --print` as a subprocess in the worktree cwd. Pass the
  prompt via stdin. Capture stdout/stderr. Timeout configurable (default 30 min).
- Two prompt templates: initial-work and review-loop. Both REQUIRE the agent to
  write/update docs/feat/<slug>.md as the work summary, commit it, and push the branch.
- Runner returns a structured AgentResult dataclass; do not return raw stdout.

Tests required:
- Worktree create/remove using a pytest tmp_path git repo fixture
- Agent runner with a fake `claude` shim (a shell script in tests/bin/)
- Slug generation edge cases (unicode, very long titles, leading/trailing dashes)
- Push failure surfaces as a typed error, not a silent return

Done when: pytest -k "worktree or agent" green; integration test creates a real
worktree, makes a commit, pushes, and cleans up.
```

### 2D — GitHub adapter

```
You own the GitHub integration. Read IMPLEMENTATION_PLAN.md sections "Phase 1" and
"Phase 2D" before starting.

Deliverables: adapters/github/client.py, GitHub webhook route, two GitHub Actions
workflow templates in templates/.

Constraints:
- All GitHub ops shell out to `gh` CLI. Parse JSON output, never scrape text.
- VCS port methods: open_draft_pr, mark_ready_for_review, enable_auto_merge, get_pr_status.
- Webhook receiver: POST /github/webhook. Two event shapes:
    {event: "deployment_ready", pr_url, deploy_url}
    {event: "merged", pr_url, merge_sha}
  Verify HMAC-SHA256 signature using GITHUB_WEBHOOK_SECRET.
- Workflow templates the user installs in their target repo:
    .github/workflows/deploy-preview.yml — on push to feat/* branches, build,
       deploy, POST deploy_url back to our webhook
    .github/workflows/notify-merge.yml — on push to main, POST merge event back

Tests required:
- gh CLI mocked via subprocess.run patching; assert correct invocations
- Webhook signature verification with valid/invalid vectors
- Webhook route routes deployment_ready to a comment on the Trello card via the
  injected BoardProvider

Done when: pytest -k github green; manual smoke test on a throwaway repo opens a PR,
marks it ready, enables auto-merge.
```

---

## Risks & gotchas

- **Trello webhook HEAD check.** First-time webhook registration fails silently if your endpoint doesn't 200 on HEAD. Test it before debugging anything else.
- **Agent infinite loops on its own comments.** The comment handler must filter out comments authored by the agent's Trello identity. Decide early: does the agent comment as you (your token) or as a separate Trello user? A separate bot user is cleaner for filtering.
- **Worktree dirty state.** If an agent run crashes mid-edit, the next run starts in a dirty worktree. Decide: reset hard on retry, or refuse to spawn? Refuse-and-alert is safer initially.
- **PR auto-merge race.** `gh pr merge --auto` requires branch protection enabled with required checks. Without that, it merges immediately. Document this requirement in the README.
- **Concurrent claude subprocesses.** 12 concurrent runs can blow your Anthropic rate limit and your laptop's RAM. Add a global semaphore (configurable, default 4) on the agent runner even though task-level locking permits more.
- **Trello rate limit:** ~300 reqs / 10s per key. Backfill on startup can hit this if you have a lot of history. Implement basic 429 backoff in the client from day one.
- **Branch naming collisions.** Two cards with similar titles can produce the same slug. Always include `short_id` as the prefix; it's per-board unique.
- **`docs/feat/<slug>.md` conflicts on rebase.** If the agent rebases on main and main has touched the same doc tree, conflicts happen. Keep the doc path keyed on `short_id` (e.g., `docs/feat/0042-add-room-detection.md`) so collisions are impossible.

## Suggested kickoff order in your Claude session

1. Open the orchestrator session in `agents-trello/` worktree on `main`.
2. Run **Phase 0** in the orchestrator (no subagent needed; ~30 min).
3. Run **Phase 1** in the orchestrator (this is design-heavy; one consistent voice). Merge to `main` when green.
4. Spawn **four subagents** for 2A/2B/2C/2D, each in its own worktree off the new `main`. Use the prompts above.
5. As each subagent finishes, review their PR in the orchestrator session, merge to `main`.
6. Run **Phase 3** in the orchestrator (composition).
7. Run **Phase 4** end-to-end on the real test board.

A reasonable target with this structure is 2–3 focused days of work, with subagents running their phases in parallel taking the calendar time down meaningfully on day 2.
