"""Adapter tests driven with lightweight fakes - no live gateway."""

from __future__ import annotations

import asyncio
from typing import Any, Optional

import disnake
import pytest

from mochi_analytics import MochiClient, MochiEvent
from mochi_analytics_disnake import attach_mochi, wrap_command


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


class FakeUser:
    def __init__(self, uid: int) -> None:
        self.id = uid


class FakeOption:
    def __init__(self, name: str, option_type: Any, options: tuple = ()) -> None:
        self.name = name
        self.type = option_type
        self.options = list(options)


class FakeData:
    """Stands in for disnake's ApplicationCommandInteractionData."""

    def __init__(
        self,
        name: str,
        command_type: Any = disnake.ApplicationCommandType.chat_input,
        options: tuple = (),
    ) -> None:
        self.name = name
        self.type = command_type
        self.options = list(options)


class FakeGuild:
    def __init__(self, gid: int, shard_id: int = 0, member_count: int = 10) -> None:
        self.id = gid
        self.shard_id = shard_id
        self.member_count = member_count
        self.name = f"guild-{gid}"


class FakeChannel:
    def __init__(self, type_name: str) -> None:
        self.type = getattr(disnake.ChannelType, type_name)


class FakeInteraction:
    def __init__(
        self,
        data: FakeData,
        guild_id: Optional[int] = 42,
        channel: Optional[FakeChannel] = None,
    ) -> None:
        self.type = disnake.InteractionType.application_command
        self.data = data
        self.guild_id = guild_id
        self.guild = FakeGuild(guild_id) if guild_id else None
        self.author = FakeUser(7)
        self.channel = channel


class FakeShard:
    def __init__(self, latency: float) -> None:
        self.latency = latency


class FakeBot:
    """Minimal stand-in exposing the surface attach_mochi touches."""

    def __init__(self) -> None:
        self.listeners: dict[str, list] = {}
        self.guilds: list[FakeGuild] = [FakeGuild(1), FakeGuild(2, member_count=5)]
        self.latency = 0.038
        self.shard_id = 0
        self.shard_count = 1

    def add_listener(self, callback, name: str) -> None:
        self.listeners.setdefault(name, []).append(callback)

    def remove_listener(self, callback, name: str) -> None:
        self.listeners.get(name, []).remove(callback)

    def is_ready(self) -> bool:
        return False

    async def dispatch(self, name: str, *args) -> None:
        for cb in self.listeners.get(name, []):
            await cb(*args)


async def test_auto_tracks_application_command():
    bot = FakeBot()
    mochi = RecordingClient()
    attach_mochi(bot, mochi)

    await bot.dispatch("on_interaction", FakeInteraction(FakeData("ping")))

    assert len(mochi.events) == 1
    event = mochi.events[0]
    assert event.type == "command"
    assert event.name == "ping"
    assert event.guild_id == "42"
    assert event.user_id == "7"
    assert event.channel_type == "guild_text"
    assert event.meta == {"source": "slash"}


async def test_subcommand_path_is_rebuilt_from_the_payload():
    bot = FakeBot()
    mochi = RecordingClient()
    attach_mochi(bot, mochi)

    data = FakeData(
        "config",
        options=(FakeOption("set", disnake.OptionType.sub_command),),
    )
    await bot.dispatch("on_interaction", FakeInteraction(data))

    assert mochi.events[0].name == "config set"


async def test_nested_subcommand_group_path():
    bot = FakeBot()
    mochi = RecordingClient()
    attach_mochi(bot, mochi)

    data = FakeData(
        "config",
        options=(
            FakeOption(
                "channel",
                disnake.OptionType.sub_command_group,
                options=(FakeOption("set", disnake.OptionType.sub_command),),
            ),
        ),
    )
    await bot.dispatch("on_interaction", FakeInteraction(data))

    assert mochi.events[0].name == "config channel set"


async def test_value_options_do_not_extend_the_name():
    bot = FakeBot()
    mochi = RecordingClient()
    attach_mochi(bot, mochi)

    data = FakeData("play", options=(FakeOption("query", disnake.OptionType.string),))
    await bot.dispatch("on_interaction", FakeInteraction(data))

    assert mochi.events[0].name == "play"


async def test_context_menu_source():
    bot = FakeBot()
    mochi = RecordingClient()
    attach_mochi(bot, mochi)

    data = FakeData("Report", command_type=disnake.ApplicationCommandType.message)
    await bot.dispatch("on_interaction", FakeInteraction(data))

    assert mochi.events[0].meta == {"source": "context_menu"}


async def test_channel_types_are_mapped():
    bot = FakeBot()
    mochi = RecordingClient()
    attach_mochi(bot, mochi)

    for type_name in ("public_thread", "stage_voice", "category"):
        await bot.dispatch(
            "on_interaction",
            FakeInteraction(FakeData("ping"), channel=FakeChannel(type_name)),
        )

    assert [e.channel_type for e in mochi.events] == ["thread", "guild_voice", "other"]


async def test_dm_interaction():
    bot = FakeBot()
    mochi = RecordingClient()
    attach_mochi(bot, mochi)

    await bot.dispatch("on_interaction", FakeInteraction(FakeData("ping"), guild_id=None))

    event = mochi.events[0]
    assert event.guild_id is None
    assert event.channel_type == "dm"


async def test_non_command_interaction_is_skipped():
    bot = FakeBot()
    mochi = RecordingClient()
    attach_mochi(bot, mochi)

    interaction = FakeInteraction(FakeData("ping"))
    interaction.type = disnake.InteractionType.component
    await bot.dispatch("on_interaction", interaction)

    assert mochi.events == []


async def test_ignore_commands_are_skipped():
    bot = FakeBot()
    mochi = RecordingClient()
    attach_mochi(bot, mochi, ignore_commands=["ping"])

    await bot.dispatch("on_interaction", FakeInteraction(FakeData("ping")))

    assert mochi.events == []


async def test_auto_track_disabled():
    bot = FakeBot()
    mochi = RecordingClient()
    attach_mochi(bot, mochi, auto_track_commands=False)

    await bot.dispatch("on_interaction", FakeInteraction(FakeData("play")))

    assert mochi.events == []


async def test_guild_join_and_leave():
    bot = FakeBot()
    mochi = RecordingClient()
    attach_mochi(bot, mochi, include_guild_names=True)

    await bot.dispatch("on_guild_join", FakeGuild(99, member_count=3))
    await bot.dispatch("on_guild_remove", FakeGuild(99))

    assert [e.type for e in mochi.events] == ["guild_join", "guild_leave"]
    assert mochi.events[0].meta["name"] == "guild-99"
    assert mochi.events[0].meta["memberCount"] == 3
    assert mochi.events[1].meta == {"name": "guild-99"}


async def test_guild_names_omitted_by_default():
    bot = FakeBot()
    mochi = RecordingClient()
    attach_mochi(bot, mochi)

    await bot.dispatch("on_guild_join", FakeGuild(99, member_count=3))

    assert mochi.events[0].meta == {"memberCount": 3}


async def test_ready_sends_one_snapshot_when_unsharded():
    bot = FakeBot()
    mochi = RecordingClient()
    attach_mochi(bot, mochi)

    await bot.dispatch("on_ready")
    await asyncio.sleep(0)

    assert len(mochi.snapshots) == 1
    snap = mochi.snapshots[0]
    assert snap.guild_count == 2
    assert snap.approximate_member_sum == 15
    assert snap.ws_ping_ms == 38


async def test_autosharded_client_snapshots_each_shard():
    bot = FakeBot()
    bot.shard_count = 2
    bot.shards = {0: FakeShard(0.038), 1: FakeShard(float("nan"))}
    bot.guilds = [FakeGuild(1, shard_id=0), FakeGuild(2, shard_id=1, member_count=5)]
    mochi = RecordingClient()
    attach_mochi(bot, mochi)

    await bot.dispatch("on_ready")
    await asyncio.sleep(0)

    assert len(mochi.snapshots) == 2
    first, second = mochi.snapshots
    assert (first.shard_id, first.guild_count, first.ws_ping_ms) == (0, 1, 38)
    # NaN latency before the first heartbeat becomes 0.
    assert (second.shard_id, second.guild_count, second.ws_ping_ms) == (1, 1, 0)
    assert second.total_shards == 2


async def test_wrap_command_records_success_and_duration():
    mochi = RecordingClient()

    @wrap_command(mochi)
    async def handler(interaction):
        return "ok"

    result = await handler(FakeInteraction(FakeData("play")))

    assert result == "ok"
    assert mochi.events[0].success is True
    assert mochi.events[0].duration_ms is not None


async def test_wrap_command_records_failure():
    mochi = RecordingClient()

    @wrap_command(mochi)
    async def handler(interaction):
        raise ValueError("boom")

    with pytest.raises(ValueError):
        await handler(FakeInteraction(FakeData("play")))

    assert mochi.events[0].success is False


async def test_detach_removes_listeners():
    bot = FakeBot()
    mochi = RecordingClient()
    detach = attach_mochi(bot, mochi)

    detach()
    await bot.dispatch("on_interaction", FakeInteraction(FakeData("play")))

    assert mochi.events == []
