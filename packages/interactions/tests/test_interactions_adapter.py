"""Adapter tests driven with lightweight fakes - no live gateway."""

from __future__ import annotations

import asyncio
from typing import Any, Optional
from unittest.mock import MagicMock

import interactions
import pytest
from interactions.api import events as ipy_events

from mochi_analytics import MochiClient, MochiEvent
from mochi_analytics_interactions import attach_mochi, wrap_command


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


class FakeCommand:
    def __init__(self, resolved_name: str) -> None:
        self.resolved_name = resolved_name
        self.name = resolved_name.split(" ")[0]


def context_menu_command(name: str) -> Any:
    """Must satisfy `isinstance(x, interactions.ContextMenu)`."""
    command = MagicMock(spec=interactions.ContextMenu)
    command.resolved_name = name
    command.name = name
    return command


class FakeChannel:
    def __init__(self, type_name: str) -> None:
        self.type = getattr(interactions.ChannelType, type_name)


class FakeGuild:
    def __init__(self, gid: int, member_count: int = 10) -> None:
        self.id = gid
        self.member_count = member_count
        self.name = f"guild-{gid}"


class FakeContext:
    def __init__(
        self,
        command: Any,
        guild_id: Optional[int] = 42,
        channel: Optional[FakeChannel] = None,
        interaction_id: int = 1000,
    ) -> None:
        self.id = interaction_id
        self.command = command
        self.guild_id = guild_id
        self.author_id = 7
        self.channel = channel


class FakeShardState:
    def __init__(self, shard_id: int, latency: float) -> None:
        self.shard_id = shard_id
        self.latency = latency


class FakeClient:
    """Minimal stand-in exposing the surface attach_mochi touches."""

    def __init__(self) -> None:
        self.listeners: dict[str, list] = {}
        self.guilds: list[FakeGuild] = [FakeGuild(1), FakeGuild(2, member_count=5)]
        self.latency = 0.038
        self.total_shards = 1
        self.is_ready = True

    def add_listener(self, listener) -> None:
        self.listeners.setdefault(listener.event, []).append(listener)

    async def dispatch(self, event_name: str, event=None) -> None:
        for listener in list(self.listeners.get(event_name, [])):
            await listener.callback(event)


def completion(ctx: FakeContext) -> Any:
    return MagicMock(ctx=ctx)


def error(ctx: FakeContext, exc: Exception) -> Any:
    return MagicMock(ctx=ctx, error=exc)


async def test_tracks_a_successful_command_on_completion():
    client = FakeClient()
    mochi = RecordingClient()
    attach_mochi(client, mochi)

    await client.dispatch("command_completion", completion(FakeContext(FakeCommand("config set"))))

    assert len(mochi.events) == 1
    event = mochi.events[0]
    assert event.type == "command"
    assert event.name == "config set"  # resolved_name includes the subcommand path
    assert event.guild_id == "42"
    assert event.user_id == "7"
    assert event.channel_type == "guild_text"
    assert event.success is True
    assert event.meta == {"source": "slash"}


async def test_a_preceding_command_error_marks_the_command_failed():
    """CommandCompletion fires in a `finally`, so it runs for failures too."""
    client = FakeClient()
    mochi = RecordingClient()
    attach_mochi(client, mochi)

    ctx = FakeContext(FakeCommand("play"))
    await client.dispatch("command_error", error(ctx, ValueError("boom")))
    await client.dispatch("command_completion", completion(ctx))

    assert len(mochi.events) == 1
    assert mochi.events[0].success is False


async def test_the_failure_marker_does_not_leak_to_the_next_invocation():
    client = FakeClient()
    mochi = RecordingClient()
    attach_mochi(client, mochi)

    failed = FakeContext(FakeCommand("play"), interaction_id=1)
    await client.dispatch("command_error", error(failed, ValueError("boom")))
    await client.dispatch("command_completion", completion(failed))

    ok = FakeContext(FakeCommand("play"), interaction_id=2)
    await client.dispatch("command_completion", completion(ok))

    assert [e.success for e in mochi.events] == [False, True]


async def test_duration_is_omitted_without_wrap_command():
    client = FakeClient()
    mochi = RecordingClient()
    attach_mochi(client, mochi)

    await client.dispatch("command_completion", completion(FakeContext(FakeCommand("play"))))

    assert mochi.events[0].duration_ms is None


async def test_context_menu_source():
    client = FakeClient()
    mochi = RecordingClient()
    attach_mochi(client, mochi)

    ctx = FakeContext(context_menu_command("Report"))
    await client.dispatch("command_completion", completion(ctx))

    assert mochi.events[0].meta == {"source": "context_menu"}


async def test_channel_types_are_mapped():
    client = FakeClient()
    mochi = RecordingClient()
    attach_mochi(client, mochi)

    for type_name in ("GUILD_PUBLIC_THREAD", "GUILD_STAGE_VOICE", "GUILD_CATEGORY"):
        ctx = FakeContext(FakeCommand("ping"), channel=FakeChannel(type_name))
        await client.dispatch("command_completion", completion(ctx))

    assert [e.channel_type for e in mochi.events] == ["thread", "guild_voice", "other"]


async def test_dm_context():
    client = FakeClient()
    mochi = RecordingClient()
    attach_mochi(client, mochi)

    ctx = FakeContext(FakeCommand("ping"), guild_id=None)
    await client.dispatch("command_completion", completion(ctx))

    assert mochi.events[0].guild_id is None
    assert mochi.events[0].channel_type == "dm"


async def test_ignore_commands_are_skipped():
    client = FakeClient()
    mochi = RecordingClient()
    attach_mochi(client, mochi, ignore_commands=["ping"])

    await client.dispatch("command_completion", completion(FakeContext(FakeCommand("ping"))))

    assert mochi.events == []


async def test_auto_track_disabled():
    client = FakeClient()
    mochi = RecordingClient()
    attach_mochi(client, mochi, auto_track_commands=False)

    await client.dispatch("command_completion", completion(FakeContext(FakeCommand("play"))))

    assert mochi.events == []


async def test_guild_join_is_ignored_until_the_client_is_ready():
    """GuildJoin also fires for every cached guild while the bot starts up."""
    client = FakeClient()
    client.is_ready = False
    mochi = RecordingClient()
    attach_mochi(client, mochi)

    await client.dispatch("guild_join", MagicMock(guild=FakeGuild(99), guild_id=99))

    assert mochi.events == []


async def test_guild_join_and_leave():
    client = FakeClient()
    mochi = RecordingClient()
    attach_mochi(client, mochi, include_guild_names=True)

    await client.dispatch(
        "guild_join", MagicMock(guild=FakeGuild(99, member_count=3), guild_id=99)
    )
    await client.dispatch("guild_left", MagicMock(guild=FakeGuild(99), guild_id=99))

    assert [e.type for e in mochi.events] == ["guild_join", "guild_leave"]
    assert mochi.events[0].meta == {"memberCount": 3, "name": "guild-99"}
    assert mochi.events[1].meta == {"name": "guild-99"}


async def test_guild_leave_without_a_cached_guild():
    client = FakeClient()
    mochi = RecordingClient()
    attach_mochi(client, mochi, include_guild_names=True)

    await client.dispatch("guild_left", MagicMock(guild=None, guild_id=99))

    assert mochi.events[0].type == "guild_leave"
    assert mochi.events[0].meta is None


async def test_startup_sends_one_snapshot_when_unsharded():
    client = FakeClient()
    mochi = RecordingClient()
    attach_mochi(client, mochi)

    await client.dispatch("startup", MagicMock())
    await asyncio.sleep(0)

    assert len(mochi.snapshots) == 1
    snap = mochi.snapshots[0]
    assert snap.guild_count == 2
    assert snap.approximate_member_sum == 15
    assert snap.ws_ping_ms == 38


async def test_autosharded_client_snapshots_each_shard():
    client = FakeClient()
    client.total_shards = 2
    client.shards = [FakeShardState(0, 0.038), FakeShardState(1, float("nan"))]
    shard_guilds = {0: [FakeGuild(1)], 1: [FakeGuild(2, member_count=5)]}
    client.get_shards_guild = lambda shard_id: shard_guilds[shard_id]
    mochi = RecordingClient()
    attach_mochi(client, mochi)

    await client.dispatch("startup", MagicMock())
    await asyncio.sleep(0)

    assert len(mochi.snapshots) == 2
    first, second = mochi.snapshots
    assert (first.shard_id, first.guild_count, first.ws_ping_ms) == (0, 1, 38)
    assert first.approximate_member_sum == 10
    # NaN latency before the shard's first heartbeat becomes 0.
    assert (second.shard_id, second.guild_count, second.ws_ping_ms) == (1, 1, 0)
    assert second.total_shards == 2


async def test_wrap_command_records_success_and_duration():
    mochi = RecordingClient()

    @wrap_command(mochi)
    async def handler(ctx):
        return "ok"

    result = await handler(FakeContext(FakeCommand("play")))

    assert result == "ok"
    assert mochi.events[0].success is True
    assert mochi.events[0].duration_ms is not None


async def test_wrap_command_records_failure():
    mochi = RecordingClient()

    @wrap_command(mochi)
    async def handler(ctx):
        raise ValueError("boom")

    with pytest.raises(ValueError):
        await handler(FakeContext(FakeCommand("play")))

    assert mochi.events[0].success is False


async def test_detach_removes_listeners():
    client = FakeClient()
    mochi = RecordingClient()
    detach = attach_mochi(client, mochi)

    detach()
    await client.dispatch("command_completion", completion(FakeContext(FakeCommand("play"))))

    assert mochi.events == []
    assert all(not listeners for listeners in client.listeners.values())


async def test_listeners_are_registered_under_the_expected_event_names():
    client = FakeClient()
    mochi = RecordingClient()
    attach_mochi(client, mochi)

    assert set(client.listeners) == {
        "command_error",
        "command_completion",
        "guild_join",
        "guild_left",
        "startup",
    }
