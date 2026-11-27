import asyncio
import datetime
import logging
import re
from typing import Any, Literal
from urllib.parse import urlparse

import aiohttp

from app.settings import DiscordConfig

logger = logging.getLogger("uc.backend.discord")

API_BASE = "https://discord.com/api/v10"

COLORS = {
    "RED": 0xFF0000,
    "GREEN": 0x00FF00,
    "YELLOW": 0xFFFF00,
    "BLUE": 0x0000FF,
    "ORANGE": 0xFFA500,
    "PURPLE": 0x800080,
}

Channel = Literal[
    "staff_pick", "visibility", "first_publish", "new_uploads", "comments", "staff_actions"
]


def escape_markdown(text: str | None) -> str:
    """Escape Discord markdown in user text.

    Args:
        text: User-provided text.

    Returns:
        str: Escaped text.
    """
    return re.sub(r"([\\*_~|`])", r"\\\1", text or "")


def embed(
    title: str | None = None,
    description: str | None = None,
    color: str | None = None,
    thumbnail: str | None = None,
    timestamp: bool = False,
) -> dict[str, Any]:
    """Build a Discord embed.

    Args:
        title: Embed title.
        description: Embed description.
        color: Color name from ``COLORS``.
        thumbnail: Thumbnail image URL.
        timestamp: Whether to stamp the current time.

    Returns:
        dict[str, Any]: The embed object.
    """
    data: dict[str, Any] = {}
    if title:
        data["title"] = title
    if description:
        data["description"] = description
    if color:
        data["color"] = COLORS[color]
    if thumbnail:
        data["thumbnail"] = {"url": thumbnail}
    if timestamp:
        data["timestamp"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
    return data or {"title": "​"}


class ChartSummary:
    """Chart details used in Discord messages.

    Attributes:
        chart_id: Chart id.
        title: Chart title.
        artists: Chart artists.
        charter: Free-text charter name with the owner's handle.
        jacket_url: Public jacket URL.
    """

    def __init__(
        self, chart_id: str, title: str, artists: str | None, charter: str, jacket_url: str
    ) -> None:
        """Create a chart summary.

        Args:
            chart_id: Chart id.
            title: Chart title.
            artists: Chart artists.
            charter: Free-text charter name with the owner's handle.
            jacket_url: Public jacket URL.
        """
        self.chart_id = chart_id
        self.title = title
        self.artists = artists
        self.charter = charter
        self.jacket_url = jacket_url


class DiscordBot:
    """Posts event messages to Discord channels with a bot token.

    Attributes:
        config: Discord settings.
        http: Shared HTTP session.
        sonolus_server_url: Public URL of the Sonolus server.
        chart_prefix: Chart item name prefix on the Sonolus server.
    """

    def __init__(
        self,
        config: DiscordConfig,
        http: aiohttp.ClientSession,
        sonolus_server_url: str,
        chart_prefix: str,
    ) -> None:
        """Create the bot client.

        Args:
            config: Discord settings.
            http: Shared HTTP session.
            sonolus_server_url: Public URL of the Sonolus server.
            chart_prefix: Chart item name prefix on the Sonolus server.
        """
        self.config = config
        self.http = http
        self.sonolus_server_url = sonolus_server_url
        self.chart_prefix = chart_prefix
        self._tasks: set[asyncio.Task] = set()

    def level_link(self, chart_id: str) -> str:
        """Build the open.sonolus.com link of a chart.

        Args:
            chart_id: Chart id.

        Returns:
            str: The link.
        """
        parsed = urlparse(self.sonolus_server_url)
        return f"https://open.sonolus.com/{parsed.netloc}{parsed.path.rstrip('/')}/levels/{self.chart_prefix}{chart_id}"

    def _channel_id(self, channel: Channel) -> str:
        """Look up the configured channel id of an event.

        Args:
            channel: Event channel name.

        Returns:
            str: Channel id, empty when the event is disabled.
        """
        return getattr(self.config.channels, channel).strip()

    async def _send(self, channel_id: str, embeds: list[dict[str, Any]]) -> None:
        """Post a message, logging instead of raising on failure.

        Args:
            channel_id: Target channel id.
            embeds: Message embeds.
        """
        try:
            async with self.http.post(
                f"{API_BASE}/channels/{channel_id}/messages",
                json={"embeds": embeds, "allowed_mentions": {"parse": []}},
                headers={"Authorization": f"Bot {self.config.bot_token}"},
                timeout=aiohttp.ClientTimeout(total=10),
            ) as response:
                if response.status >= 300:
                    logger.warning(
                        "discord post failed: %s %s", response.status, await response.text()
                    )
        except (aiohttp.ClientError, asyncio.TimeoutError) as error:
            logger.warning("discord post failed: %r", error)

    def post(self, channel: Channel, embeds: list[dict[str, Any]]) -> None:
        """Queue a message in the background; skipped when the bot or channel isn't configured.

        Args:
            channel: Event channel name.
            embeds: Message embeds.
        """
        channel_id = self._channel_id(channel)
        if not self.config.bot_token.strip() or not channel_id:
            return
        task = asyncio.create_task(self._send(channel_id, embeds))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def drain(self) -> None:
        """Wait for queued messages before shutdown."""
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)

    def staff_pick(self, chart: ChartSummary) -> None:
        """Announce a new staff pick.

        Args:
            chart: The picked chart.
        """
        self.post(
            "staff_pick",
            [
                embed(description="## 🏆 新着 StaffPick! / New Staff Pick!", color="PURPLE"),
                embed(
                    title=escape_markdown(chart.title),
                    description=(
                        f"- *{escape_markdown(chart.artists)}*\n"
                        f"譜面作者 / Charted by: `{escape_markdown(chart.charter)}`\n\n"
                        f"今すぐプレイ！ / Play it now!\n{self.level_link(chart.chart_id)}"
                    ),
                    color="YELLOW",
                    thumbnail=chart.jacket_url,
                    timestamp=True,
                ),
            ],
        )

    def visibility_changed(self, chart: ChartSummary, previous: str, new: str, actor: str) -> None:
        """Announce a visibility change.

        Args:
            chart: The chart.
            previous: Previous status.
            new: New status.
            actor: Who made the change.
        """
        color = "RED" if new == "PRIVATE" else "ORANGE" if new == "UNLISTED" else "GREEN"
        self.post(
            "visibility",
            [
                embed(
                    title="Chart visibility change",
                    description=(
                        f"The chart `{escape_markdown(chart.title)}` (`{escape_markdown(chart.charter)}`) "
                        f"was changed to `{new}` from `{previous}`.\n\n"
                        f"{self.level_link(chart.chart_id)}\n\nAction by `{escape_markdown(actor)}`"
                    ),
                    color=color,
                    thumbnail=chart.jacket_url,
                    timestamp=True,
                )
            ],
        )

    def schedule_changed(
        self, chart: ChartSummary, publish_seconds: int | None, actor: str
    ) -> None:
        """Announce a scheduled publish being set or removed.

        Args:
            chart: The chart.
            publish_seconds: New publish time in epoch seconds, or None when removed.
            actor: Who made the change.
        """
        if publish_seconds is None:
            description = (
                f"Scheduled publish was **removed** for chart `{escape_markdown(chart.title)}` "
                f"(`{escape_markdown(chart.charter)}`).\n\n"
                f"{self.level_link(chart.chart_id)}\n\nAction by `{escape_markdown(actor)}`"
            )
            color = "ORANGE"
        else:
            description = (
                f"New **scheduled publish** set for chart `{escape_markdown(chart.title)}` "
                f"(`{escape_markdown(chart.charter)}`).\n\n"
                f"Publish time: <t:{publish_seconds}:F>  (<t:{publish_seconds}:R>)\n\n"
                f"{self.level_link(chart.chart_id)}\n\nAction by `{escape_markdown(actor)}`"
            )
            color = "GREEN"
        self.post(
            "visibility",
            [
                embed(
                    title="Chart schedule updated",
                    description=description,
                    color=color,
                    thumbnail=chart.jacket_url,
                    timestamp=True,
                )
            ],
        )

    def first_publish(self, chart: ChartSummary) -> None:
        """Announce a chart published for the first time.

        Args:
            chart: The chart.
        """
        self.post(
            "first_publish",
            [
                embed(
                    title=escape_markdown(chart.title),
                    description=(
                        f"- *{escape_markdown(chart.artists)}*\n"
                        f"譜面作者 / Charted by: `{escape_markdown(chart.charter)}`\n\n"
                        f"新着譜面 / New chart!\n{self.level_link(chart.chart_id)}"
                    ),
                    color="GREEN",
                    thumbnail=chart.jacket_url,
                    timestamp=True,
                )
            ],
        )

    def new_upload(self, chart: ChartSummary, uploader: str) -> None:
        """Announce a new upload.

        Args:
            chart: The uploaded chart.
            uploader: Uploading account.
        """
        self.post(
            "new_uploads",
            [
                embed(
                    title="New upload",
                    description=(
                        f"`{escape_markdown(chart.title)}` (`{escape_markdown(chart.charter)}`) "
                        f"was uploaded by `{escape_markdown(uploader)}`.\n\n{self.level_link(chart.chart_id)}"
                    ),
                    color="BLUE",
                    thumbnail=chart.jacket_url,
                    timestamp=True,
                )
            ],
        )

    def new_comment(self, chart: ChartSummary, commenter: str, content: str) -> None:
        """Announce a new comment.

        Args:
            chart: The commented chart.
            commenter: Commenting account.
            content: Comment text.
        """
        self.post(
            "comments",
            [
                embed(
                    title="New comment",
                    description=(
                        f"`{escape_markdown(commenter)}` on `{escape_markdown(chart.title)}` "
                        f"(`{escape_markdown(chart.charter)}`):\n\n>>> {escape_markdown(content[:3500])}\n\n"
                        f"{self.level_link(chart.chart_id)}"
                    ),
                    color="BLUE",
                    thumbnail=chart.jacket_url,
                    timestamp=True,
                )
            ],
        )

    def staff_action(
        self,
        actor: str,
        action: str,
        target_type: str,
        target_id: str,
        previous_value: str | None,
        new_value: str | None,
    ) -> None:
        """Announce a staff action.

        Args:
            actor: Acting account or the admin key.
            action: Action name.
            target_type: Kind of target.
            target_id: Target id.
            previous_value: Value before the action.
            new_value: Value after the action.
        """
        lines = [
            f"Action: `{escape_markdown(action)}`",
            f"Target: `{escape_markdown(target_type)}` `{escape_markdown(target_id)}`",
        ]
        if previous_value is not None:
            lines.append(f"Before: `{escape_markdown(previous_value[:500])}`")
        if new_value is not None:
            lines.append(f"After: `{escape_markdown(new_value[:500])}`")
        lines.append(f"Action by `{escape_markdown(actor)}`")
        self.post(
            "staff_actions",
            [
                embed(
                    title="Staff action",
                    description="\n".join(lines),
                    color="PURPLE",
                    timestamp=True,
                )
            ],
        )
