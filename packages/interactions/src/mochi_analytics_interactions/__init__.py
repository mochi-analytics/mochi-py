"""interactions.py adapter for Mochi analytics.

Auto-instruments application commands, guild joins/leaves, and periodic
health snapshots. Mirrors ``mochi-analytics-discordpy``, adapted to
interactions.py's ``Listener`` model.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, Awaitable, Callable, Dict, Iterable, List, Optional

import interactions
from interactions import Listener
from interactions.api import events as ipy_events
from mochi_analytics import MochiClient, MochiEvent, MochiSnapshot

__all__ = ["attach_mochi", "wrap_command", "MochiClient"]

__version__ = "0.2.0"  # x-release-please-version

_HOUR = 60 * 60

#: Keyed by ``ChannelType`` member name so a new member added upstream degrades
#: to "other" rather than raising.
_CHANNEL_TYPE_BY_NAME = {
    "GUILD_TEXT": "guild_text",
    "GUILD_NEWS": "guild_text",
    "DM": "dm",
    "GROUP_DM": "group_dm",
    "GUILD_VOICE": "guild_voice",
    "GUILD_STAGE_VOICE": "guild_voice",
    "GUILD_NEWS_THREAD": "thread",
    "GUILD_PUBLIC_THREAD": "thread",
    "GUILD_PRIVATE_THREAD": "thread",
}


def attach_mochi(
    client: interactions.Client,
    mochi: MochiClient,
    *,
    include_guild_names: bool = False,
    ignore_commands: Iterable[str] = (),
    snapshot_interval: float = _HOUR,
    auto_track_commands: bool = True,
) -> Callable[[], None]:
    """Hook a :class:`MochiClient` into an interactions.py client.

    Works with ``interactions.Client`` and ``interactions.AutoShardedClient``.
    Returns a ``detach`` callable that removes every listener and timer it
    installed.

    Commands are recorded on ``CommandCompletion``, which interactions.py
    dispatches in a ``finally`` - so it fires for failures too. A preceding
    ``CommandError`` is what marks an invocation unsuccessful. Duration is not
    reported by either event; apply :func:`wrap_command` if you need it.

    :param include_guild_names: put guild names in join/leave metadata.
    :param ignore_commands: command names to skip entirely.
    :param snapshot_interval: seconds between guild-count snapshots (default 1h).
    :param auto_track_commands: when ``False``, command events are not recorded
        automatically - use :func:`wrap_command` for accurate success/duration.
    """
    ignored = set(ignore_commands)
    snapshot_task: Optional[asyncio.Task[None]] = None
    #: Invocations that raised, keyed by interaction id, awaiting completion.
    failed: Dict[Any, bool] = {}

    async def on_command_error(event: ipy_events.CommandError) -> None:
        if not auto_track_commands:
            return
        failed[_context_key(event.ctx)] = True

    async def on_command_completion(event: ipy_events.CommandCompletion) -> None:
        if not auto_track_commands:
            return
        ctx = event.ctx
        command = getattr(ctx, "command", None)
        success = not failed.pop(_context_key(ctx), False)
        if command is None:
            return
        if getattr(command, "name", None) in ignored:
            return
        mochi.track(
            MochiEvent(
                type="command",
                name=_full_command_name(ctx),
                guild_id=_str_or_none(getattr(ctx, "guild_id", None)),
                user_id=_str_or_none(getattr(ctx, "author_id", None)),
                channel_type=_channel_type_of(ctx),
                success=success,
                meta={"source": _command_source(ctx)},
            )
        )

    async def on_guild_join(event: ipy_events.GuildJoin) -> None:
        # GuildJoin also fires for every cached guild while the bot starts up.
        if not client.is_ready:
            return
        guild = event.guild
        meta: dict[str, Any] = {"memberCount": getattr(guild, "member_count", None)}
        if include_guild_names:
            meta["name"] = guild.name
        mochi.track(
            MochiEvent(
                type="guild_join",
                guild_id=str(event.guild_id),
                shard_id=_shard_id_of(client, event.guild_id),
                meta=meta,
            )
        )

    async def on_guild_left(event: ipy_events.GuildLeft) -> None:
        # `guild` is only populated when the guild was still cached.
        guild = event.guild
        meta = (
            {"name": guild.name} if include_guild_names and guild is not None else None
        )
        mochi.track(
            MochiEvent(
                type="guild_leave",
                guild_id=str(event.guild_id),
                shard_id=_shard_id_of(client, event.guild_id),
                meta=meta,
            )
        )

    async def send_snapshot() -> None:
        for snapshot in _snapshots(client):
            await mochi.snapshot(snapshot)

    async def snapshot_loop() -> None:
        try:
            await send_snapshot()
            while True:
                await asyncio.sleep(snapshot_interval)
                await send_snapshot()
        except asyncio.CancelledError:
            pass

    async def on_startup(_event: ipy_events.Startup) -> None:
        nonlocal snapshot_task
        if snapshot_task is None or snapshot_task.done():
            snapshot_task = asyncio.get_running_loop().create_task(snapshot_loop())

    registered = [
        Listener.create(ipy_events.CommandError)(on_command_error),
        Listener.create(ipy_events.CommandCompletion)(on_command_completion),
        Listener.create(ipy_events.GuildJoin)(on_guild_join),
        Listener.create(ipy_events.GuildLeft)(on_guild_left),
        Listener.create(ipy_events.Startup)(on_startup),
    ]
    for listener in registered:
        client.add_listener(listener)

    def detach() -> None:
        for listener in registered:
            bucket = client.listeners.get(listener.event)
            if bucket and listener in bucket:
                bucket.remove(listener)
        if snapshot_task is not None:
            snapshot_task.cancel()

    return detach


def wrap_command(
    mochi: MochiClient,
    handler: Optional[Callable[..., Awaitable[Any]]] = None,
) -> Callable[..., Awaitable[Any]]:
    """Wrap a command callback so Mochi records accurate duration & success.

    Use together with ``auto_track_commands=False``, otherwise each invocation
    is recorded twice. Works both as a two-arg wrapper and as a decorator
    factory::

        handler = wrap_command(mochi, play)      # explicit

        @interactions.slash_command()
        @wrap_command(mochi)                      # decorator
        async def play(ctx: interactions.SlashContext):
            ...

    The wrapped callback must take the context as its first argument.
    """

    def decorate(fn: Callable[..., Awaitable[Any]]) -> Callable[..., Awaitable[Any]]:
        async def wrapped(ctx: interactions.BaseContext, *args: Any, **kwargs: Any) -> Any:
            started_at = time.monotonic()
            success = True
            try:
                return await fn(ctx, *args, **kwargs)
            except Exception:
                success = False
                raise
            finally:
                mochi.track(
                    MochiEvent(
                        type="command",
                        name=_full_command_name(ctx),
                        guild_id=_str_or_none(getattr(ctx, "guild_id", None)),
                        user_id=_str_or_none(getattr(ctx, "author_id", None)),
                        channel_type=_channel_type_of(ctx),
                        success=success,
                        duration_ms=round((time.monotonic() - started_at) * 1000),
                        meta={"source": _command_source(ctx)},
                    )
                )

        return wrapped

    return decorate if handler is None else decorate(handler)


# -- helpers ------------------------------------------------------------


def _context_key(ctx: Any) -> Any:
    """CommandError and CommandCompletion carry the same context object."""
    return getattr(ctx, "id", None) or id(ctx)


def _full_command_name(ctx: Any) -> str:
    command = getattr(ctx, "command", None)
    if command is None:
        return ""
    # resolved_name spans any group/subcommand path, e.g. "config set".
    return getattr(command, "resolved_name", None) or getattr(command, "name", "")


def _command_source(ctx: Any) -> str:
    command = getattr(ctx, "command", None)
    return "context_menu" if isinstance(command, interactions.ContextMenu) else "slash"


def _channel_type_of(ctx: Any) -> str:
    channel = getattr(ctx, "channel", None)
    if channel is None:
        return "guild_text" if getattr(ctx, "guild_id", None) else "dm"
    channel_type = getattr(channel, "type", None)
    return _CHANNEL_TYPE_BY_NAME.get(getattr(channel_type, "name", ""), "other")


def _total_shards_of(client: interactions.Client) -> int:
    count = getattr(client, "total_shards", None)
    return count if isinstance(count, int) and count > 0 else 1


def _shard_id_of(client: interactions.Client, guild_id: Optional[Any]) -> int:
    get_shard_id = getattr(client, "get_shard_id", None)  # AutoShardedClient only
    if callable(get_shard_id) and guild_id is not None:
        try:
            return int(get_shard_id(guild_id))
        except Exception:  # a bad shard count must never break tracking
            return 0
    shard_id = getattr(client, "shard_id", None)
    return shard_id if isinstance(shard_id, int) else 0


def _ping_ms(latency: Optional[float]) -> int:
    """Latency is seconds, and NaN or inf until the first heartbeat lands."""
    if latency is None or latency != latency or latency in (float("inf"), float("-inf")):
        return 0
    return max(0, round(latency * 1000))


def _snapshots(client: interactions.Client) -> List[MochiSnapshot]:
    """One snapshot per shard owned by this process.

    ``guildCount`` is defined as a shard's *local* guild count, so an
    AutoShardedClient - which holds every shard in one process - reports each
    shard separately rather than one process-wide total.
    """
    total_shards = _total_shards_of(client)
    shards = getattr(client, "shards", None)  # AutoShardedClient only
    get_shards_guild = getattr(client, "get_shards_guild", None)

    if not shards or not callable(get_shards_guild):
        guilds = list(client.guilds)
        return [
            MochiSnapshot(
                guild_count=len(guilds),
                shard_id=_shard_id_of(client, None),
                total_shards=total_shards,
                approximate_member_sum=sum(_member_count(g) for g in guilds),
                ws_ping_ms=_ping_ms(client.latency),
            )
        ]

    snapshots: List[MochiSnapshot] = []
    for state in shards:
        shard_id = getattr(state, "shard_id", 0)
        guilds = get_shards_guild(shard_id)
        snapshots.append(
            MochiSnapshot(
                guild_count=len(guilds),
                shard_id=shard_id,
                total_shards=total_shards,
                approximate_member_sum=sum(_member_count(g) for g in guilds),
                ws_ping_ms=_ping_ms(getattr(state, "latency", None)),
            )
        )
    return snapshots


def _member_count(guild: Any) -> int:
    return getattr(guild, "member_count", 0) or 0


def _str_or_none(value: Any) -> Optional[str]:
    return str(value) if value is not None else None
