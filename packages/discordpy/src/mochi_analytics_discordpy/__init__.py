"""discord.py adapter for Mochi analytics.

Auto-instruments application commands, guild joins/leaves, and periodic
health snapshots. Mirrors ``@mochi-analytics/discordjs``.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, Awaitable, Callable, Iterable, Optional

import discord
from mochi_analytics import MochiClient, MochiEvent, MochiSnapshot

__all__ = ["attach_mochi", "wrap_command", "MochiClient"]

__version__ = "1.1.0"  # x-release-please-version

_HOUR = 60 * 60


def attach_mochi(
    client: discord.Client,
    mochi: MochiClient,
    *,
    include_guild_names: bool = False,
    ignore_commands: Iterable[str] = (),
    snapshot_interval: float = _HOUR,
    auto_track_commands: bool = True,
) -> Callable[[], None]:
    """Hook a :class:`MochiClient` into a discord.py client.

    Works with ``discord.Client``, ``discord.AutoShardedClient`` and
    ``commands.Bot``. Returns a ``detach`` callable that removes every
    listener and timer it installed.

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
        base_name = getattr(command, "name", None)
        if base_name in ignored:
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
        await mochi.snapshot(
            MochiSnapshot(
                guild_count=len(client.guilds),
                shard_id=_shard_id_of(client, None),
                total_shards=_total_shards_of(client),
                approximate_member_sum=sum(g.member_count or 0 for g in client.guilds),
                ws_ping_ms=max(0, round(client.latency * 1000))
                if client.latency == client.latency  # guard against NaN before ready
                else 0,
            )
        )

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
    wrapper (mirroring the JS ``wrapHandler``) and as a decorator factory::

        handler = wrap_command(mochi, play)      # explicit

        @app_commands.command()
        @wrap_command(mochi)                      # decorator
        async def play(interaction: discord.Interaction):
            ...

    The wrapped callback must take the interaction as its first argument.
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
                mochi.track(
                    MochiEvent(
                        type="command",
                        name=_full_command_name(interaction),
                        guild_id=_str_or_none(interaction.guild_id),
                        user_id=str(interaction.user.id) if interaction.user else None,
                        channel_type=_channel_type_of(interaction),
                        success=success,
                        duration_ms=round((time.monotonic() - started_at) * 1000),
                        meta={"source": "slash"},
                    )
                )

        return wrapped

    return decorate if handler is None else decorate(handler)


# -- helpers ------------------------------------------------------------


def _add_listener(client: discord.Client, callback: Callable, name: str) -> None:
    # commands.Bot / ext supports add_listener; plain Client does not.
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
    command = interaction.command
    if command is None:
        return ""
    # qualified_name includes any group/subcommand path, e.g. "config set".
    return getattr(command, "qualified_name", getattr(command, "name", ""))


def _command_source(interaction: discord.Interaction) -> str:
    data = interaction.data or {}
    # Context-menu commands are type 2 (user) or 3 (message); slash is 1.
    return "slash" if data.get("type", 1) == 1 else "context_menu"


def _channel_type_of(interaction: discord.Interaction) -> str:
    channel = interaction.channel
    if channel is None:
        return "guild_text" if interaction.guild_id else "dm"
    ct = getattr(channel, "type", None)
    mapping = {
        discord.ChannelType.private: "dm",
        discord.ChannelType.group: "group_dm",
        discord.ChannelType.voice: "guild_voice",
        discord.ChannelType.stage_voice: "guild_voice",
        discord.ChannelType.public_thread: "thread",
        discord.ChannelType.private_thread: "thread",
        discord.ChannelType.news_thread: "thread",
        discord.ChannelType.text: "guild_text",
        discord.ChannelType.news: "guild_text",
    }
    return mapping.get(ct, "other")


def _shard_id_of(client: discord.Client, guild: Optional[discord.Guild]) -> int:
    if guild is not None and guild.shard_id is not None:
        return guild.shard_id
    shard_id = getattr(client, "shard_id", None)
    return shard_id if isinstance(shard_id, int) else 0


def _total_shards_of(client: discord.Client) -> int:
    count = getattr(client, "shard_count", None)
    return count if isinstance(count, int) and count > 0 else 1


def _str_or_none(value: Any) -> Optional[str]:
    return str(value) if value is not None else None
