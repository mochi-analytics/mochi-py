"""hikari adapter for Mochi analytics.

Auto-instruments application commands, guild joins/leaves, and periodic
health snapshots. Mirrors ``mochi-analytics-discordpy``, adapted to hikari's
event-bus model.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, Awaitable, Callable, Iterable, List, Optional

import hikari
from hikari import snowflakes
from mochi_analytics import MochiClient, MochiEvent, MochiSnapshot

__all__ = ["attach_mochi", "wrap_command", "MochiClient"]

__version__ = "0.1.0"  # x-release-please-version

_HOUR = 60 * 60

_SUB_COMMAND = 1
_SUB_COMMAND_GROUP = 2

#: Keyed by ``ChannelType`` member name so a new member added upstream degrades
#: to "other" rather than raising.
_CHANNEL_TYPE_BY_NAME = {
    "GUILD_TEXT": "guild_text",
    "GUILD_NEWS": "guild_text",
    "DM": "dm",
    "GROUP_DM": "group_dm",
    "GUILD_VOICE": "guild_voice",
    "GUILD_STAGE": "guild_voice",
    "GUILD_NEWS_THREAD": "thread",
    "GUILD_PUBLIC_THREAD": "thread",
    "GUILD_PRIVATE_THREAD": "thread",
}


def attach_mochi(
    bot: hikari.GatewayBot,
    mochi: MochiClient,
    *,
    include_guild_names: bool = False,
    ignore_commands: Iterable[str] = (),
    snapshot_interval: float = _HOUR,
    auto_track_commands: bool = True,
) -> Callable[[], None]:
    """Hook a :class:`MochiClient` into a hikari ``GatewayBot``.

    Returns a ``detach`` callable that unsubscribes every listener and cancels
    the snapshot loop.

    hikari runs every shard inside one process, so one snapshot is sent per
    shard carrying that shard's own guild count.

    :param include_guild_names: put guild names in join/leave metadata.
    :param ignore_commands: command names to skip entirely.
    :param snapshot_interval: seconds between guild-count snapshots (default 1h).
    :param auto_track_commands: when ``False``, command events are not recorded
        automatically - use :func:`wrap_command` for accurate success/duration.
    """
    ignored = set(ignore_commands)
    snapshot_task: Optional[asyncio.Task[None]] = None

    async def on_interaction(event: hikari.InteractionCreateEvent) -> None:
        if not auto_track_commands:
            return
        interaction = event.interaction
        if not isinstance(interaction, hikari.CommandInteraction):
            return
        if interaction.command_name in ignored:
            return
        mochi.track(
            MochiEvent(
                type="command",
                name=_full_command_name(interaction),
                guild_id=_str_or_none(interaction.guild_id),
                user_id=str(interaction.user.id) if interaction.user else None,
                channel_type=_channel_type_of(interaction),
                shard_id=_shard_id_of(bot, interaction.guild_id),
                meta={"source": _command_source(interaction)},
            )
        )

    async def on_guild_join(event: hikari.GuildJoinEvent) -> None:
        guild = event.guild
        meta: dict[str, Any] = {"memberCount": guild.member_count}
        if include_guild_names:
            meta["name"] = guild.name
        mochi.track(
            MochiEvent(
                type="guild_join",
                guild_id=str(event.guild_id),
                shard_id=_event_shard_id(event),
                meta=meta,
            )
        )

    async def on_guild_leave(event: hikari.GuildLeaveEvent) -> None:
        # old_guild is only populated when the guild was still cached.
        old_guild = event.old_guild
        meta = (
            {"name": old_guild.name}
            if include_guild_names and old_guild is not None
            else None
        )
        mochi.track(
            MochiEvent(
                type="guild_leave",
                guild_id=str(event.guild_id),
                shard_id=_event_shard_id(event),
                meta=meta,
            )
        )

    async def send_snapshot() -> None:
        for snapshot in _snapshots(bot):
            await mochi.snapshot(snapshot)

    async def snapshot_loop() -> None:
        try:
            await send_snapshot()
            while True:
                await asyncio.sleep(snapshot_interval)
                await send_snapshot()
        except asyncio.CancelledError:
            pass

    async def on_started(_event: hikari.StartedEvent) -> None:
        nonlocal snapshot_task
        if snapshot_task is None or snapshot_task.done():
            snapshot_task = asyncio.get_running_loop().create_task(snapshot_loop())

    subscriptions = [
        (hikari.InteractionCreateEvent, on_interaction),
        (hikari.GuildJoinEvent, on_guild_join),
        (hikari.GuildLeaveEvent, on_guild_leave),
        (hikari.StartedEvent, on_started),
    ]
    for event_type, callback in subscriptions:
        bot.subscribe(event_type, callback)

    def detach() -> None:
        for event_type, callback in subscriptions:
            bot.unsubscribe(event_type, callback)
        if snapshot_task is not None:
            snapshot_task.cancel()

    return detach


def wrap_command(
    mochi: MochiClient,
    handler: Optional[Callable[..., Awaitable[Any]]] = None,
) -> Callable[..., Awaitable[Any]]:
    """Wrap a command callback so Mochi records accurate duration & success.

    Use together with ``auto_track_commands=False``. Works both as a two-arg
    wrapper and as a decorator factory::

        handler = wrap_command(mochi, play)      # explicit

        @wrap_command(mochi)                      # decorator
        async def play(interaction: hikari.CommandInteraction):
            ...

    The wrapped callback must take the :class:`hikari.CommandInteraction` as
    its first argument.
    """

    def decorate(fn: Callable[..., Awaitable[Any]]) -> Callable[..., Awaitable[Any]]:
        async def wrapped(
            interaction: hikari.CommandInteraction, *args: Any, **kwargs: Any
        ) -> Any:
            started_at = time.monotonic()
            success = True
            try:
                return await fn(interaction, *args, **kwargs)
            except Exception:
                success = False
                raise
            finally:
                mochi.track(
                    MochiEvent(
                        type="command",
                        name=_full_command_name(interaction),
                        guild_id=_str_or_none(interaction.guild_id),
                        user_id=str(interaction.user.id) if interaction.user else None,
                        channel_type=_channel_type_of(interaction),
                        success=success,
                        duration_ms=round((time.monotonic() - started_at) * 1000),
                        meta={"source": _command_source(interaction)},
                    )
                )

        return wrapped

    return decorate if handler is None else decorate(handler)


# -- helpers ------------------------------------------------------------


def _full_command_name(interaction: hikari.CommandInteraction) -> str:
    """Build e.g. "config set" by walking the interaction's option tree."""
    parts = [interaction.command_name or ""]
    options = interaction.options or []
    while options:
        nested = next(
            (o for o in options if _option_type(o) in (_SUB_COMMAND, _SUB_COMMAND_GROUP)),
            None,
        )
        if nested is None:
            break
        parts.append(getattr(nested, "name", "") or "")
        options = getattr(nested, "options", None) or []
    return " ".join(part for part in parts if part)


def _option_type(option: Any) -> int:
    option_type = getattr(option, "type", None)
    if option_type is None:
        return -1
    return getattr(option_type, "value", option_type)


def _command_source(interaction: hikari.CommandInteraction) -> str:
    command_type = interaction.command_type
    value = getattr(command_type, "value", command_type)
    # CommandType.SLASH is 1; USER (2) and MESSAGE (3) are context menus.
    return "slash" if value == 1 else "context_menu"


def _channel_type_of(interaction: hikari.CommandInteraction) -> str:
    channel = interaction.channel
    if channel is None:
        return "guild_text" if interaction.guild_id else "dm"
    channel_type = getattr(channel, "type", None)
    return _CHANNEL_TYPE_BY_NAME.get(getattr(channel_type, "name", ""), "other")


def _event_shard_id(event: Any) -> int:
    shard = getattr(event, "shard", None)
    shard_id = getattr(shard, "id", None)
    return shard_id if isinstance(shard_id, int) else 0


def _total_shards_of(bot: hikari.GatewayBot, shard_count: int) -> int:
    count = getattr(bot, "shard_count", 0)
    if isinstance(count, int) and count > 0:
        return count
    return shard_count or 1


def _shard_id_of(bot: hikari.GatewayBot, guild_id: Optional[Any]) -> int:
    """hikari does not put a shard on the interaction, so derive it."""
    if guild_id is None:
        return 0
    total_shards = _total_shards_of(bot, len(getattr(bot, "shards", None) or {}))
    try:
        return snowflakes.calculate_shard_id(total_shards, guild_id)
    except Exception:  # a bad shard count must never break tracking
        return 0


def _ping_ms(latency: Optional[float]) -> int:
    """Latency is seconds, and NaN until the shard's first heartbeat lands."""
    if latency is None or latency != latency or latency in (float("inf"), float("-inf")):
        return 0
    return max(0, round(latency * 1000))


def _snapshots(bot: hikari.GatewayBot) -> List[MochiSnapshot]:
    """One snapshot per shard owned by this process."""
    guilds = bot.cache.get_guilds_view()
    shards = getattr(bot, "shards", None) or {}
    total_shards = _total_shards_of(bot, len(shards))

    if not shards:
        return [
            MochiSnapshot(
                guild_count=len(guilds),
                shard_id=0,
                total_shards=total_shards,
                approximate_member_sum=sum(g.member_count or 0 for g in guilds.values()),
                ws_ping_ms=_ping_ms(getattr(bot, "heartbeat_latency", None)),
            )
        ]

    owned: dict[int, list[Any]] = {shard_id: [] for shard_id in shards}
    for guild_id, guild in guilds.items():
        shard_id = snowflakes.calculate_shard_id(total_shards, guild_id)
        if shard_id in owned:
            owned[shard_id].append(guild)

    snapshots: List[MochiSnapshot] = []
    for shard_id, shard in sorted(shards.items()):
        shard_guilds = owned[shard_id]
        snapshots.append(
            MochiSnapshot(
                guild_count=len(shard_guilds),
                shard_id=shard_id,
                total_shards=total_shards,
                approximate_member_sum=sum(g.member_count or 0 for g in shard_guilds),
                ws_ping_ms=_ping_ms(getattr(shard, "heartbeat_latency", None)),
            )
        )
    return snapshots


def _str_or_none(value: Any) -> Optional[str]:
    return str(value) if value is not None else None
