"""CLI commands: register-webhook, list-cards, replay."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys

from agents_trello.config import Config
from agents_trello.project import ProjectConfig


def _pick_project(config: Config, project_id: str | None) -> ProjectConfig:
    if project_id is None:
        if not config.projects:
            raise SystemExit("No projects configured.")
        return config.projects[0]
    for proj in config.projects:
        if proj.id == project_id:
            return proj
    raise SystemExit(f"Unknown project id: {project_id}")


async def _register_webhook(config: Config, project: ProjectConfig) -> None:
    from agents_trello.adapters.trello.client import TrelloClient

    client = TrelloClient(config.trello_api_key, config.trello_api_token)
    callback_url = f"{config.webhook_base_url}/trello/webhook"
    try:
        webhook_id = await client.create_webhook(project.trello_board_id, callback_url)
        print(f"Webhook registered for project {project.id}: {webhook_id}")
        print(f"Callback URL: {callback_url}")
    finally:
        await client.close()


async def _list_cards(config: Config, project: ProjectConfig) -> None:
    from agents_trello.adapters.trello.client import TrelloClient
    from agents_trello.adapters.trello.provider import TrelloBoardProvider

    client = TrelloClient(config.trello_api_key, config.trello_api_token)
    try:
        provider = await TrelloBoardProvider.create(client, project.trello_board_id)
        tasks = await provider.list_tasks()
        for task in tasks:
            print(f"[{task.column.value:12s}] {task.short_id:>4s} | {task.title}")
        if not tasks:
            print(f"No cards found on board for project {project.id}.")
    finally:
        await client.close()


async def _replay_event(config: Config, project: ProjectConfig, event_json_str: str) -> None:
    from agents_trello.adapters.trello.client import TrelloClient
    from agents_trello.adapters.trello.mapping import action_to_events
    from agents_trello.adapters.trello.provider import TrelloBoardProvider

    client = TrelloClient(config.trello_api_key, config.trello_api_token)
    try:
        provider = await TrelloBoardProvider.create(client, project.trello_board_id)
        action = json.loads(event_json_str)
        events = action_to_events(action, provider._list_id_to_column)
        for event in events:
            print(f"Event: {type(event).__name__}")
            print(f"  action_id: {event.meta.action_id}")
            print(f"  timestamp: {event.meta.timestamp}")
            print(f"  fields: {event}")
    finally:
        await client.close()


def _list_projects(config: Config) -> None:
    if not config.projects:
        print("No projects configured.")
        return
    for proj in config.projects:
        print(f"{proj.id:20s} {proj.name:30s} board={proj.trello_board_id} repo={proj.github_repo}")


def main() -> None:
    parser = argparse.ArgumentParser(prog="agents-trello", description="Trello agent CLI")
    parser.add_argument(
        "--project", help="Project id from projects.yaml (defaults to first)", default=None
    )
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("register-webhook", help="Register Trello webhook for the board")
    sub.add_parser("list-cards", help="List all cards on the board")
    sub.add_parser("list-projects", help="List configured projects")

    replay_parser = sub.add_parser("replay-event", help="Replay a Trello action JSON")
    replay_parser.add_argument("event_json", help="JSON string of a Trello action")

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(1)

    config = Config.from_env()

    if args.command == "list-projects":
        _list_projects(config)
        return

    project = _pick_project(config, args.project)

    if args.command == "register-webhook":
        asyncio.run(_register_webhook(config, project))
    elif args.command == "list-cards":
        asyncio.run(_list_cards(config, project))
    elif args.command == "replay-event":
        asyncio.run(_replay_event(config, project, args.event_json))


if __name__ == "__main__":
    main()
