"""Push notifications via ntfy.sh."""

from __future__ import annotations

import logging

import httpx

logger = logging.getLogger(__name__)


class NotifyService:
    def __init__(self, topic: str) -> None:
        self._topic = topic
        self._url = f"https://ntfy.sh/{topic}"
        self._client = httpx.AsyncClient(timeout=10.0)

    async def send(self, title: str, message: str, tags: str = "") -> None:
        if not self._topic:
            return
        try:
            headers: dict[str, str] = {"Title": title}
            if tags:
                headers["Tags"] = tags
            await self._client.post(self._url, content=message, headers=headers)
            logger.info("notification_sent", extra={"title": title})
        except Exception:
            logger.warning("notification_failed", extra={"title": title}, exc_info=True)

    async def pr_opened(self, task_title: str, pr_url: str) -> None:
        await self.send(
            title=f"PR ready: {task_title}",
            message=f"Review the PR:\n{pr_url}",
            tags="eyes,git",
        )

    async def review_responded(self, task_title: str, card_url: str) -> None:
        await self.send(
            title=f"Agent responded: {task_title}",
            message=f"Check the response:\n{card_url}",
            tags="speech_balloon",
        )

    async def agent_started(self, task_title: str) -> None:
        await self.send(
            title=f"Agent working: {task_title}",
            message=f'Agent picked up "{task_title}"',
            tags="hammer",
        )

    async def agent_failed(self, task_title: str, error: str) -> None:
        await self.send(
            title=f"Agent failed: {task_title}",
            message=error[:200],
            tags="warning",
        )

    async def branch_pushed(self, task_title: str, branch: str) -> None:
        await self.send(
            title=f"Branch pushed: {task_title}",
            message=f"Branch {branch} is live",
            tags="rocket",
        )

    async def close(self) -> None:
        await self._client.aclose()
