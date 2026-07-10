"""Adapter tests driven with lightweight fakes - no live gateway."""

from __future__ import annotations

import asyncio
from typing import Any, Optional
from unittest.mock import MagicMock

import hikari
import pytest

from mochi_analytics import MochiClient, MochiEvent
from mochi_analytics_hikari import attach_mochi, wrap_command

#: Guild ids shard as `(id >> 22) % shard_count`, so this one lands on shard 1
#: of a two-shard bot while a small id lands on shard 0.
SHARD_1_GUILD = 1 << 22


class RecordingClient(MochiClient):
    """A MochiClient that records events instead of sending them."""

    def __init__(self) -> None:
        super().__init__("http://localhost", "k", transport=self._noop)
        self.events: list[MochiEvent] = []
        self.snapshots: list[Any] = []

    async def _noop(self, url: str, body: Any):  # pragma: no cover
        return 202, "{}"

    def track(self, event: MochiEvent) -> None:  # type: ignore[override]
        self.events.append(event)

    async def snapshot(self, snapshot: Any) -> None:  # type: ignore[override]
        self.snapshots.append(snapshot)


class FakeGuild:
    def __init__(self, gid: int, member_count: Optional[int] = 10) -> None:
        self.id = gid
        self.member_count = member_count
        self.name = f"guild-{gid}"


class FakeShard:
    def __init__(self, shard_id: int, heartbeat_latency: float) -> None:
        self.id = shard_id
        self.heartbeat_latency = heartbeat_latency


class FakeCache:
    def __init__(self, guilds: dict[int, FakeGuild]) -> None:
        self._guilds = guilds

    def get_guilds_view(self) -> dict[int, FakeGuild]:
        return self._guilds


class FakeBot:
    """Minimal stand-in exposing the surface attach_mochi touches."""

    def __init__(self, guilds: Optional[dict[int, FakeGuild]] = None) -> None:
        self.subscriptions: dict[Any, list] = {}
        self.cache = FakeCache(guilds if guilds is not None else {1: FakeGuild(1)})
        self.shards: dict[int, FakeShard] = {}
        self.shard_count = 1
        self.heartbeat_latency = 0.038

    def subscribe(self, event_type, callback) -> None:
        self.subscriptions.setdefault(event_type, []).append(callback)

    def unsubscribe(self, event_type, callback) -> None:
        self.subscriptions.get(event_type, []).remove(callback)

    async def dispatch(self, event_type, event=None) -> None:
        for cb in self.subscriptions.get(event_type, []):
            await cb(event)


def command_interaction(
    command_name: str = "ping",
    *,
    guild_id: Optional[int] = 42,
    command_type: Any = hikari.CommandType.SLASH,
    channel_type: Optional[Any] = hikari.ChannelType.GUILD_TEXT,
    options: Optional[list] = None,
) -> Any:
    """A stand-in that still satisfies `isinstance(x, hikari.CommandInteraction)`."""
    interaction = MagicMock(spec=hikari.CommandInteraction)
    interaction.command_name = command_name
    interaction.command_type = command_type
    interaction.guild_id = guild_id
    interaction.user = MagicMock(id=7)
    interaction.options = options
    if channel_type is None:
        interaction.channel = None
    else:
        interaction.channel = MagicMock(type=channel_type)
    return interaction


def option(name: str, option_type: Any, options: Optional[list] = None) -> Any:
    mock = MagicMock(type=option_type, options=options)
    mock.name = name  # `name` is reserved by the MagicMock constructor.
    return mock


def interaction_event(interaction: Any) -> Any:
    return MagicMock(interaction=interaction)


async def test_auto_tracks_application_command():
    bot = FakeBot()
    mochi = RecordingClient()
    attach_mochi(bot, mochi)

    await bot.dispatch(hikari.InteractionCreateEvent, interaction_event(command_interaction()))

    assert len(mochi.events) == 1
    event = mochi.events[0]
    assert event.type == "command"
    assert event.name == "ping"
    assert event.guild_id == "42"
    assert event.user_id == "7"
    assert event.channel_type == "guild_text"
    assert event.meta == {"source": "slash"}


async def test_subcommand_path_is_walked():
    bot = FakeBot()
    mochi = RecordingClient()
    attach_mochi(bot, mochi)

    sub = option("set", hikari.OptionType.SUB_COMMAND)
    group = option("channel", hikari.OptionType.SUB_COMMAND_GROUP, options=[sub])
    interaction = command_interaction("config", options=[group])
    await bot.dispatch(hikari.InteractionCreateEvent, interaction_event(interaction))

    assert mochi.events[0].name == "config channel set"


async def test_value_options_do_not_extend_the_name():
    bot = FakeBot()
    mochi = RecordingClient()
    attach_mochi(bot, mochi)

    interaction = command_interaction(
        "play", options=[option("query", hikari.OptionType.STRING)]
    )
    await bot.dispatch(hikari.InteractionCreateEvent, interaction_event(interaction))

    assert mochi.events[0].name == "play"


async def test_context_menu_source():
    bot = FakeBot()
    mochi = RecordingClient()
    attach_mochi(bot, mochi)

    interaction = command_interaction("Report", command_type=hikari.CommandType.MESSAGE)
    await bot.dispatch(hikari.InteractionCreateEvent, interaction_event(interaction))

    assert mochi.events[0].meta == {"source": "context_menu"}


async def test_channel_types_are_mapped():
    bot = FakeBot()
    mochi = RecordingClient()
    attach_mochi(bot, mochi)

    for channel_type in (
        hikari.ChannelType.GUILD_PUBLIC_THREAD,
        hikari.ChannelType.GUILD_STAGE,
        hikari.ChannelType.GUILD_CATEGORY,
    ):
        await bot.dispatch(
            hikari.InteractionCreateEvent,
            interaction_event(command_interaction(channel_type=channel_type)),
        )

    assert [e.channel_type for e in mochi.events] == ["thread", "guild_voice", "other"]


async def test_dm_interaction():
    bot = FakeBot()
    mochi = RecordingClient()
    attach_mochi(bot, mochi)

    interaction = command_interaction(guild_id=None, channel_type=None)
    await bot.dispatch(hikari.InteractionCreateEvent, interaction_event(interaction))

    event = mochi.events[0]
    assert event.guild_id is None
    assert event.channel_type == "dm"
    assert event.shard_id == 0


async def test_shard_id_is_derived_from_the_guild_id():
    bot = FakeBot()
    bot.shard_count = 2
    bot.shards = {0: FakeShard(0, 0.01), 1: FakeShard(1, 0.01)}
    mochi = RecordingClient()
    attach_mochi(bot, mochi)

    await bot.dispatch(
        hikari.InteractionCreateEvent,
        interaction_event(command_interaction(guild_id=SHARD_1_GUILD)),
    )

    assert mochi.events[0].shard_id == 1


async def test_non_command_interactions_are_skipped():
    bot = FakeBot()
    mochi = RecordingClient()
    attach_mochi(bot, mochi)

    await bot.dispatch(
        hikari.InteractionCreateEvent,
        interaction_event(MagicMock(spec=hikari.ComponentInteraction)),
    )

    assert mochi.events == []


async def test_ignore_commands_are_skipped():
    bot = FakeBot()
    mochi = RecordingClient()
    attach_mochi(bot, mochi, ignore_commands=["ping"])

    await bot.dispatch(hikari.InteractionCreateEvent, interaction_event(command_interaction()))

    assert mochi.events == []


async def test_auto_track_disabled():
    bot = FakeBot()
    mochi = RecordingClient()
    attach_mochi(bot, mochi, auto_track_commands=False)

    await bot.dispatch(hikari.InteractionCreateEvent, interaction_event(command_interaction()))

    assert mochi.events == []


async def test_guild_join_and_leave():
    bot = FakeBot()
    mochi = RecordingClient()
    attach_mochi(bot, mochi, include_guild_names=True)

    join = MagicMock(guild=FakeGuild(99, member_count=3), guild_id=99, shard=FakeShard(1, 0.01))
    leave = MagicMock(guild_id=99, old_guild=FakeGuild(99), shard=FakeShard(1, 0.01))
    await bot.dispatch(hikari.GuildJoinEvent, join)
    await bot.dispatch(hikari.GuildLeaveEvent, leave)

    assert [e.type for e in mochi.events] == ["guild_join", "guild_leave"]
    assert mochi.events[0].shard_id == 1
    assert mochi.events[0].meta == {"memberCount": 3, "name": "guild-99"}
    assert mochi.events[1].meta == {"name": "guild-99"}


async def test_guild_leave_without_a_cached_guild():
    bot = FakeBot()
    mochi = RecordingClient()
    attach_mochi(bot, mochi, include_guild_names=True)

    leave = MagicMock(guild_id=99, old_guild=None, shard=FakeShard(0, 0.01))
    await bot.dispatch(hikari.GuildLeaveEvent, leave)

    assert mochi.events[0].type == "guild_leave"
    assert mochi.events[0].meta is None


async def test_started_sends_one_snapshot_when_unsharded():
    bot = FakeBot(guilds={1: FakeGuild(1), 2: FakeGuild(2, member_count=5)})
    mochi = RecordingClient()
    attach_mochi(bot, mochi)

    await bot.dispatch(hikari.StartedEvent, MagicMock())
    await asyncio.sleep(0)

    assert len(mochi.snapshots) == 1
    snap = mochi.snapshots[0]
    assert snap.guild_count == 2
    assert snap.approximate_member_sum == 15
    assert snap.ws_ping_ms == 38


async def test_snapshots_each_shard():
    bot = FakeBot(guilds={1: FakeGuild(1), SHARD_1_GUILD: FakeGuild(SHARD_1_GUILD, 5)})
    bot.shard_count = 2
    bot.shards = {0: FakeShard(0, 0.038), 1: FakeShard(1, float("nan"))}
    mochi = RecordingClient()
    attach_mochi(bot, mochi)

    await bot.dispatch(hikari.StartedEvent, MagicMock())
    await asyncio.sleep(0)

    assert len(mochi.snapshots) == 2
    first, second = mochi.snapshots
    assert (first.shard_id, first.guild_count, first.ws_ping_ms) == (0, 1, 38)
    assert first.approximate_member_sum == 10
    # NaN latency before the shard's first heartbeat becomes 0.
    assert (second.shard_id, second.guild_count, second.ws_ping_ms) == (1, 1, 0)
    assert second.approximate_member_sum == 5
    assert second.total_shards == 2


async def test_wrap_command_records_success_and_duration():
    mochi = RecordingClient()

    @wrap_command(mochi)
    async def handler(interaction):
        return "ok"

    result = await handler(command_interaction("play"))

    assert result == "ok"
    assert mochi.events[0].success is True
    assert mochi.events[0].duration_ms is not None


async def test_wrap_command_records_failure():
    mochi = RecordingClient()

    @wrap_command(mochi)
    async def handler(interaction):
        raise ValueError("boom")

    with pytest.raises(ValueError):
        await handler(command_interaction("play"))

    assert mochi.events[0].success is False


async def test_detach_unsubscribes_every_listener():
    bot = FakeBot()
    mochi = RecordingClient()
    detach = attach_mochi(bot, mochi)

    detach()
    await bot.dispatch(hikari.InteractionCreateEvent, interaction_event(command_interaction()))

    assert mochi.events == []
    assert all(not callbacks for callbacks in bot.subscriptions.values())
