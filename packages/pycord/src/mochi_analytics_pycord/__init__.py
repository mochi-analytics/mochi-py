"""Py-cord adapter for Mochi analytics.

Auto-instruments application commands, guild joins/leaves, and periodic
health snapshots. Mirrors ``mochi-analytics-discordpy``.

Py-cord installs itself under the ``discord`` import name, exactly as
discord.py does, so the two cannot be installed into the same environment.
Install ``mochi-analytics-pycord`` *or* ``mochi-analytics-discordpy``, never both.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, Awaitable, Callable, Iterable, List, Optional

import discord
from mochi_analytics import MochiClient, MochiEvent, MochiSnapshot

__all__ = ["attach_mochi", "wrap_command", "MochiClient"]

__version__ = "0.2.0"  # x-release-please-version

_HOUR = 60 * 60

_SUB_COMMAND = 1
_SUB_COMMAND_GROUP = 2

#: Keyed by ``ChannelType`` member name rather than the enum member itself:
#: py-cord and its discord.py siblings do not agree on which members exist
#: (py-cord has ``directory`` where discord.py has ``guild_directory``).
_CHANNEL_TYPE_BY_NAME = {
    "text": "guild_text",
    "news": "guild_text",
    "private": "dm",
    "group": "group_dm",
    "voice": "guild_voice",
    "stage_voice": "guild_voice",
    "news_thread": "thread",
    "public_thread": "thread",
    "private_thread": "thread",
}


def attach_mochi(
    client: discord.Client,
    mochi: MochiClient,
    *,
    include_guild_names: bool = False,
    ignore_commands: Iterable[str] = (),
    snapshot_interval: float = _HOUR,
    auto_track_commands: bool = True,
) -> Callable[[], None]:
    """Hook a :class:`MochiClient` into a Py-cord client.

    Works with ``discord.Client``, ``discord.AutoShardedClient`` and
    ``discord.Bot``. Returns a ``detach`` callable that removes every listener
    and timer it installed.

    :param include_guild_names: put guild names in join/leave metadata.
    :param ignore_commands: command names to skip entirely.
    :param snapshot_interval: seconds between guild-count snapshots (default 1h).
    :param auto_track_commands: when ``False``, command events are not recorded
        automatically - use :func:`wrap_command` for accurate success/duration.
    """
    ignored = set(ignore_commands)
    snapshot_task: Optional[asyncio.Task[None]] = None

    async def on_interaction(interaction: discord.Interaction) -> None:
        if not auto_track_commands:
            return
        if interaction.type is not discord.InteractionType.application_command:
            return
        command = interaction.command
        if command is None:
            return
        if getattr(command, "name", None) in ignored:
            return
        mochi.track(
            MochiEvent(
                type="command",
                name=_full_command_name(interaction),
                guild_id=_str_or_none(interaction.guild_id),
                user_id=str(interaction.user.id) if interaction.user else None,
                channel_type=_channel_type_of(interaction),
                shard_id=_shard_id_of(client, interaction.guild),
                meta={"source": _command_source(interaction)},
            )
        )

    async def on_guild_join(guild: discord.Guild) -> None:
        meta: dict[str, Any] = {"memberCount": guild.member_count}
        if include_guild_names:
            meta["name"] = guild.name
        mochi.track(
            MochiEvent(
                type="guild_join",
                guild_id=str(guild.id),
                shard_id=_shard_id_of(client, guild),
                meta=meta,
            )
        )

    async def on_guild_remove(guild: discord.Guild) -> None:
        mochi.track(
            MochiEvent(
                type="guild_leave",
                guild_id=str(guild.id),
                shard_id=_shard_id_of(client, guild),
                meta={"name": guild.name} if include_guild_names else None,
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

    async def on_ready() -> None:
        nonlocal snapshot_task
        if snapshot_task is None or snapshot_task.done():
            snapshot_task = asyncio.get_running_loop().create_task(snapshot_loop())

    listeners = [
        ("on_interaction", on_interaction),
        ("on_guild_join", on_guild_join),
        ("on_guild_remove", on_guild_remove),
        ("on_ready", on_ready),
    ]
    for name, callback in listeners:
        _add_listener(client, callback, name)

    if client.is_ready():
        asyncio.get_event_loop().create_task(on_ready())

    def detach() -> None:
        for name, callback in listeners:
            _remove_listener(client, callback, name)
        if snapshot_task is not None:
            snapshot_task.cancel()

    return detach


def wrap_command(
    mochi: MochiClient,
    handler: Optional[Callable[..., Awaitable[Any]]] = None,
) -> Callable[..., Awaitable[Any]]:
    """Wrap an app-command callback so Mochi records accurate duration & success.

    Use together with ``auto_track_commands=False``. Works both as a two-arg
    wrapper and as a decorator factory::

        handler = wrap_command(mochi, play)      # explicit

        @bot.slash_command()
        @wrap_command(mochi)                      # decorator
        async def play(ctx: discord.ApplicationContext):
            ...

    The wrapped callback must take the interaction (or an
    ``ApplicationContext``) as its first argument.
    """

    def decorate(fn: Callable[..., Awaitable[Any]]) -> Callable[..., Awaitable[Any]]:
        async def wrapped(interaction: discord.Interaction, *args: Any, **kwargs: Any) -> Any:
            started_at = time.monotonic()
            success = True
            try:
                return await fn(interaction, *args, **kwargs)
            except Exception:
                success = False
                raise
            finally:
                # An ApplicationContext wraps the interaction it was built from.
                target = getattr(interaction, "interaction", interaction)
                mochi.track(
                    MochiEvent(
                        type="command",
                        name=_full_command_name(target),
                        guild_id=_str_or_none(target.guild_id),
                        user_id=str(target.user.id) if target.user else None,
                        channel_type=_channel_type_of(target),
                        success=success,
                        duration_ms=round((time.monotonic() - started_at) * 1000),
                        meta={"source": _command_source(target)},
                    )
                )

        return wrapped

    return decorate if handler is None else decorate(handler)


# -- helpers ------------------------------------------------------------


def _add_listener(client: discord.Client, callback: Callable, name: str) -> None:
    add = getattr(client, "add_listener", None)
    if callable(add):
        add(callback, name)
    else:  # fall back to registering on the client's event dispatch
        client.event(callback)  # type: ignore[arg-type]


def _remove_listener(client: discord.Client, callback: Callable, name: str) -> None:
    remove = getattr(client, "remove_listener", None)
    if callable(remove):
        remove(callback, name)


def _full_command_name(interaction: discord.Interaction) -> str:
    command = getattr(interaction, "command", None)
    if command is None:
        return _name_from_data(interaction)
    # qualified_name spans any group/subcommand path, e.g. "config set".
    return getattr(command, "qualified_name", None) or getattr(command, "name", "")


def _name_from_data(interaction: discord.Interaction) -> str:
    """Fallback when the command object is not attached to the interaction."""
    data = getattr(interaction, "data", None) or {}
    if not isinstance(data, dict):
        return ""
    parts = [data.get("name", "")]
    options = data.get("options") or []
    while options:
        nested = next(
            (o for o in options if o.get("type") in (_SUB_COMMAND, _SUB_COMMAND_GROUP)),
            None,
        )
        if nested is None:
            break
        parts.append(nested.get("name", ""))
        options = nested.get("options") or []
    return " ".join(part for part in parts if part)


def _command_source(interaction: discord.Interaction) -> str:
    data = getattr(interaction, "data", None) or {}
    raw = data.get("type", 1) if isinstance(data, dict) else getattr(data, "type", 1)
    # Context-menu commands are type 2 (user) or 3 (message); slash is 1.
    return "slash" if getattr(raw, "value", raw) == 1 else "context_menu"


def _channel_type_of(interaction: discord.Interaction) -> str:
    channel = interaction.channel
    if channel is None:
        return "guild_text" if interaction.guild_id else "dm"
    channel_type = getattr(channel, "type", None)
    return _CHANNEL_TYPE_BY_NAME.get(getattr(channel_type, "name", ""), "other")


def _shard_id_of(client: discord.Client, guild: Optional[discord.Guild]) -> int:
    if guild is not None and guild.shard_id is not None:
        return guild.shard_id
    shard_id = getattr(client, "shard_id", None)
    return shard_id if isinstance(shard_id, int) else 0


def _total_shards_of(client: discord.Client) -> int:
    count = getattr(client, "shard_count", None)
    return count if isinstance(count, int) and count > 0 else 1


def _ping_ms(latency: Optional[float]) -> int:
    """Latency is seconds, and NaN or inf until the first heartbeat lands."""
    if latency is None or latency != latency or latency in (float("inf"), float("-inf")):
        return 0
    return max(0, round(latency * 1000))


def _snapshots(client: discord.Client) -> List[MochiSnapshot]:
    """One snapshot per shard owned by this process.

    ``guildCount`` is defined as a shard's *local* guild count, so an
    AutoShardedClient - which holds every shard in one process - reports each
    shard separately rather than one process-wide total.
    """
    total_shards = _total_shards_of(client)
    shards = getattr(client, "shards", None)
    if not isinstance(shards, dict) or not shards:
        guilds = list(client.guilds)
        return [
            MochiSnapshot(
                guild_count=len(guilds),
                shard_id=_shard_id_of(client, None),
                total_shards=total_shards,
                approximate_member_sum=sum(g.member_count or 0 for g in guilds),
                ws_ping_ms=_ping_ms(client.latency),
            )
        ]

    snapshots: List[MochiSnapshot] = []
    for shard_id, info in sorted(shards.items()):
        guilds = [g for g in client.guilds if g.shard_id == shard_id]
        snapshots.append(
            MochiSnapshot(
                guild_count=len(guilds),
                shard_id=shard_id,
                total_shards=total_shards,
                approximate_member_sum=sum(g.member_count or 0 for g in guilds),
                ws_ping_ms=_ping_ms(getattr(info, "latency", None)),
            )
        )
    return snapshots


def _str_or_none(value: Any) -> Optional[str]:
    return str(value) if value is not None else None
