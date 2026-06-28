# CLAUDE.md

## Rules

- Always print full URLs. Never truncate URLs with `...` or abbreviate them. Show the complete URL so it can be clicked or copied.
- Always restart the app after making code changes (kill port 8000, restart uvicorn).
- Run `make lint && make test` after every code change before declaring it done.
- Never silently swallow exceptions. Every `except` block must log the error with `logger.error` or `logger.warning`. Never write `except Exception: pass`.
