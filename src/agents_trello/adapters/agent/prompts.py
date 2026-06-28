"""Agent prompt templates for initial work and review loop."""

from __future__ import annotations

TRELLO_CONTEXT = """\

### Trello Context
- **Board:** {board_url}
- **This ticket:** {card_url}
- **Ticket ID:** {card_id}

### Trello API (create follow-up tickets if needed)
You can create new tickets or view the board via the orchestrator API at {api_base}:

```bash
# View the board
curl -s {api_base}/api/board | python3 -m json.tool

# Create a new ticket (goes to Backlog)
curl -s -X POST "{api_base}/api/cards?title=Fix+something&description=Details+here"

# Create and move to Todo (so it gets picked up automatically)
curl -s -X POST "{api_base}/api/cards?title=Fix+something&description=Details+here&column=Todo"

# Move an existing card to Todo
curl -s -X POST {api_base}/api/cards/CARD_ID/move-to-todo
```

Only create follow-up tickets when the current task reveals work that should be \
done separately (e.g. a bug found during implementation, a missing dependency, \
a refactor that's out of scope). Do NOT create tickets for the current task.\
"""

INITIAL_WORK_PROMPT = (
    """\
You are an autonomous coding agent. Read the task below carefully \
and implement the requested changes.

## Task: {task_title}

### Description
{task_description}

### Attached Files
{attachments}
"""
    + TRELLO_CONTEXT
    + """

### Instructions
1. Read the task description carefully and understand the requirements.
2. Review any attached files for context, reference implementations, or specs.
3. Implement the requested changes in the codebase.
4. If you discover related work that should be a separate task, create a Trello ticket for it.
5. Write or update `docs/feat/{slug}.md` with a summary of the work you performed.
6. Commit all changes with a descriptive commit message.
7. Push the branch to the remote repository.

Do your best work. Be thorough and make sure all changes are correct before committing.\
"""
)

REVIEW_PROMPT = (
    """\
You are an autonomous coding agent. You previously worked on this task and now \
have feedback to address.

## Task: {task_title}

### Original Description
{task_description}

### Attached Files
{attachments}
"""
    + TRELLO_CONTEXT
    + """

### Comment History
{comments}

### Instructions
1. Read the full comment history above. Comments prefixed with "claude: " are yours.
2. Review any attached files for additional context.
3. Focus on the latest human feedback (comments WITHOUT "claude: " prefix).
4. Make the requested changes in the codebase.
5. If you discover related work that should be a separate task, create a Trello ticket for it.
6. Update `docs/feat/{slug}.md` to reflect any changes made.
7. Commit all changes with a descriptive commit message.
8. Push the branch to the remote repository.

Address the feedback thoroughly. Make sure all changes are correct before committing.\
"""
)


CHAT_PROMPT = (
    """\
You are an AI assistant responding to a comment on a Trello card. \
Answer the question or provide the requested analysis. \
Do NOT write code, create files, or make commits. Just respond with text.

## Card: {task_title}

### Description
{task_description}

### Attached Files
{attachments}
"""
    + TRELLO_CONTEXT
    + """

### Comment History
{comments}

### Instructions
1. Read the card description and comment history.
2. Answer the latest human comment (comments WITHOUT "claude: " prefix).
3. If the comment asks you to break down work, create Trello tickets using the API.
4. Keep your response concise and actionable.
5. Do NOT create branches, worktrees, or PRs for Backlog cards.\
"""
)


def format_attachments(attachments: list[tuple[str, str]]) -> str:
    """Format attachment list into a prompt-friendly string."""
    if not attachments:
        return "No attachments."
    parts: list[str] = []
    for filename, content in attachments:
        ext = filename.rsplit(".", 1)[-1] if "." in filename else ""
        parts.append(f"#### {filename}\n```{ext}\n{content}\n```")
    return "\n\n".join(parts)
