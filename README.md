# agents-trello

Agent-driven Trello to Code pipeline. Cards move through Trello columns; agents pick them up, write code, open PRs, and respond to review feedback.

## Quickstart

```bash
# Install dependencies (requires uv)
make install

# Copy and fill in environment variables
cp .env.example .env
# Edit .env with your keys

# Run lint and tests
make lint
make test

# Start the webhook server
make run
```

## Board Setup

Create a Trello board with exactly these six lists:
- `Backlog`
- `Todo`
- `InProgress`
- `Review`
- `Merge`
- `Done`

## Architecture

- **Domain layer** (`domain/`): models, events, provider protocol, handlers. No adapter imports.
- **Adapters** (`adapters/`): Trello, GitHub, worktree, agent runner implementations.
- **Infra** (`infra/`): SQLite persistence, locks, logging.
- **Composition** (`app.py`): wires everything together with FastAPI.

## Requirements

- Python 3.12+
- `uv` for dependency management
- `gh` CLI for GitHub operations
- `git` for worktree management
- Public HTTPS endpoint for webhooks (ngrok / Cloudflare Tunnel)
