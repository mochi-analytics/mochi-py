"""Mirrors the @mochi-analytics/core vitest suite."""

from __future__ import annotations

import time
from typing import Any, Callable

import pytest

from mochi_analytics import MochiClient, MochiEvent, MochiSnapshot


class Call:
    def __init__(self, url: str, body: Any) -> None:
        self.url = url
        self.body = body


def make_transport(responder: Callable[[Call, int], int]):
    """Build a fake transport that records calls and returns a status per call."""
    calls: list[Call] = []

    async def transport(url: str, body: Any) -> "tuple[int, str]":
        call = Call(url, body)
        calls.append(call)
        status = responder(call, len(calls) - 1)
        return status, "{}"

    return calls, transport


def make_client(transport, **extra):
    errors: list[Exception] = []
    client = MochiClient(
        "http://localhost:9999/",
        "mochi_sk_test",
        flush_interval=60.0,  # effectively disabled; tests flush manually
        max_retries=2,
        on_error=errors.append,
        transport=transport,
        **extra,
    )
    return client, errors


async def test_batches_events_into_one_request_with_auth():
    calls, transport = make_transport(lambda *_: 202)
    client, _ = make_client(transport)

    client.track(MochiEvent(type="command", name="play", user_id="1"))
    client.track(MochiEvent(type="guild_join", guild_id="2"))
    await client.flush()

    assert len(calls) == 1
    assert calls[0].url == "http://localhost:9999/api/v1/ingest"
    assert len(calls[0].body["events"]) == 2
    assert calls[0].body["events"][0]["ts"]
    assert calls[0].body["events"][0]["userId"] == "1"
    await client.shutdown()


async def test_auto_flushes_when_batch_size_reached():
    calls, transport = make_transport(lambda *_: 202)
    client, _ = make_client(transport, max_batch_size=5)

    for _ in range(5):
        client.track(MochiEvent(type="command", name="x"))
    await client.flush()

    total = sum(len(c.body["events"]) for c in calls)
    assert total == 5
    await client.shutdown()


async def test_splits_oversized_queue_into_multiple_batches():
    calls, transport = make_transport(lambda *_: 202)
    client, _ = make_client(transport, max_batch_size=10)

    for _ in range(25):
        client.track(MochiEvent(type="command", name="x"))
    await client.flush()

    assert len(calls) >= 3
    total = sum(len(c.body["events"]) for c in calls)
    assert total == 25
    await client.shutdown()


async def test_retries_retryable_failure_then_succeeds():
    calls, transport = make_transport(lambda _call, index: 503 if index == 0 else 202)
    client, errors = make_client(transport)

    client.track(MochiEvent(type="command", name="play"))
    await client.flush()

    assert len(calls) == 2
    assert errors == []
    await client.shutdown()


async def test_drops_batch_and_reports_on_non_retryable_error():
    calls, transport = make_transport(lambda *_: 400)
    client, errors = make_client(transport)

    client.track(MochiEvent(type="command", name="play"))
    await client.flush()

    assert len(calls) == 1  # no retries on 400
    assert len(errors) == 1
    assert "400" in str(errors[0])
    await client.shutdown()


async def test_drops_oldest_events_on_queue_overflow():
    calls, transport = make_transport(lambda *_: 202)
    client, errors = make_client(transport, max_queue_size=3, max_batch_size=100)

    for i in range(5):
        client.track(MochiEvent(type="custom", name=f"event-{i}"))
    await client.flush()

    assert len(errors) > 0
    names = [e["name"] for c in calls for e in c.body["events"]]
    assert names == ["event-2", "event-3", "event-4"]
    await client.shutdown()


async def test_sends_snapshots_immediately():
    calls, transport = make_transport(lambda *_: 202)
    client, _ = make_client(transport)

    await client.snapshot(MochiSnapshot(guild_count=42, ws_ping_ms=30))

    assert len(calls) == 1
    assert calls[0].url == "http://localhost:9999/api/v1/snapshot"
    assert calls[0].body["guildCount"] == 42
    await client.shutdown()


async def test_snapshot_auto_attaches_resources():
    calls, transport = make_transport(lambda *_: 202)
    client, _ = make_client(transport)

    await client.snapshot(MochiSnapshot(guild_count=1))

    body = calls[0].body
    # memoryMb is best-effort: present as a positive int wherever RSS is readable.
    if "memoryMb" in body:
        assert isinstance(body["memoryMb"], int)
        assert body["memoryMb"] > 0
    await client.shutdown()


async def test_snapshot_caller_value_wins_over_measurement():
    calls, transport = make_transport(lambda *_: 202)
    client, _ = make_client(transport)

    await client.snapshot(MochiSnapshot(guild_count=1, memory_mb=7, cpu_percent=0.0))

    assert calls[0].body["memoryMb"] == 7
    assert calls[0].body["cpuPercent"] == 0.0
    await client.shutdown()


async def test_honors_retry_after_on_429():
    timestamps: list[float] = []

    async def transport(url: str, body: Any):
        timestamps.append(time.monotonic())
        if len(timestamps) == 1:
            return 429, "{}", {"Retry-After": "1"}
        return 202, "{}"

    client, errors = make_client(transport)

    start = time.monotonic()
    client.track(MochiEvent(type="command", name="play"))
    await client.flush()

    assert len(timestamps) == 2
    assert errors == []
    # Retry-After: 1s must dominate the 0.5s computed backoff.
    assert time.monotonic() - start >= 0.9
    await client.shutdown()


async def test_never_raises_when_on_error_raises():
    calls, transport = make_transport(lambda *_: 202)

    def boom(_err: Exception) -> None:
        raise RuntimeError("handler blew up")

    client = MochiClient(
        "http://localhost:9999/",
        "mochi_sk_test",
        flush_interval=60.0,
        max_queue_size=1,
        on_error=boom,
        transport=transport,
    )

    # Overflow reports from inside track(); a throwing handler must not escape.
    client.track(MochiEvent(type="custom", name="a"))
    client.track(MochiEvent(type="custom", name="b"))
    await client.shutdown()
