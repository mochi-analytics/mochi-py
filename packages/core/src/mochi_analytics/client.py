"""Batching, non-blocking analytics client for Mochi.

Failures never propagate into the caller: analytics must not be able to crash
a bot. Mirrors the design of ``@mochi-analytics/core`` but is asyncio-native.
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Optional

try:  # aiohttp is optional at import time so tests can inject a transport.
    import aiohttp
except ImportError:  # pragma: no cover - exercised only without aiohttp
    aiohttp = None  # type: ignore[assignment]

MochiEventType = str  # "command" | "guild_join" | "guild_leave" | "error" | "custom"
MochiChannelType = str  # "guild_text" | "guild_voice" | "thread" | "dm" | "group_dm" | "other"

RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})

#: A transport sends one request and returns ``(status_code, body_text)`` or
#: ``(status_code, body_text, headers)``. Headers are optional and only used to
#: read ``Retry-After`` on a 429. The default uses aiohttp; tests inject a fake.
Transport = Callable[[str, Any], Awaitable[Any]]


class MochiError(Exception):
    """Raised internally on a permanent request failure; routed to ``on_error``."""


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _read_rss_mb() -> Optional[int]:
    """Current resident set size in whole megabytes, best-effort.

    Tries psutil (accurate, cross-platform current RSS), then Linux /proc, then
    ``resource.ru_maxrss`` (peak RSS) as a last resort. Returns None if none work.
    """
    try:
        import psutil  # type: ignore

        return round(psutil.Process().memory_info().rss / 1_048_576)
    except Exception:
        pass
    try:  # Linux: field 2 of /proc/self/statm is resident pages.
        with open("/proc/self/statm") as fh:
            rss_pages = int(fh.read().split()[1])
        return round(rss_pages * os.sysconf("SC_PAGE_SIZE") / 1_048_576)
    except Exception:
        pass
    try:  # ru_maxrss is peak, not current: KiB on Linux, bytes on macOS/BSD.
        import resource

        maxrss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        divisor = 1_048_576 if sys.platform == "darwin" else 1024
        return round(maxrss / divisor)
    except Exception:
        return None


def _header(headers: Any, name: str) -> Optional[str]:
    """Case-insensitive header lookup tolerant of plain dicts and CIMultiDicts."""
    if headers is None:
        return None
    getter = getattr(headers, "get", None)
    if getter is None:
        return None
    value = getter(name)
    if value is None:
        value = getter(name.lower())
    return value


def _parse_retry_after(value: Optional[str]) -> Optional[float]:
    """Parse a Retry-After header (delta-seconds or HTTP-date) into seconds."""
    if not value:
        return None
    value = value.strip()
    try:
        seconds = float(value)
        return seconds if seconds >= 0 else None
    except ValueError:
        pass
    from email.utils import parsedate_to_datetime

    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if when is None:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return max((when - datetime.now(timezone.utc)).total_seconds(), 0.0)


@dataclass
class MochiEvent:
    """A single analytics event.

    Attributes use idiomatic snake_case; they are serialized to the camelCase
    wire format by :meth:`to_wire`.
    """

    type: MochiEventType
    name: Optional[str] = None
    guild_id: Optional[str] = None
    #: Raw Discord user id - hashed server-side with a per-bot salt, never stored.
    user_id: Optional[str] = None
    channel_type: Optional[MochiChannelType] = None
    shard_id: Optional[int] = None
    success: Optional[bool] = None
    duration_ms: Optional[int] = None
    meta: Optional[dict[str, Any]] = None
    #: ISO 8601 timestamp; defaults to send time when omitted.
    ts: Optional[str] = None

    def to_wire(self) -> dict[str, Any]:
        wire: dict[str, Any] = {"type": self.type}
        if self.name is not None:
            wire["name"] = self.name
        if self.guild_id is not None:
            wire["guildId"] = self.guild_id
        if self.user_id is not None:
            wire["userId"] = self.user_id
        if self.channel_type is not None:
            wire["channelType"] = self.channel_type
        if self.shard_id is not None:
            wire["shardId"] = self.shard_id
        if self.success is not None:
            wire["success"] = self.success
        if self.duration_ms is not None:
            wire["durationMs"] = self.duration_ms
        if self.meta is not None:
            wire["meta"] = self.meta
        wire["ts"] = self.ts or _iso_now()
        return wire


@dataclass
class MochiSnapshot:
    """A guild-count / health sample."""

    guild_count: int
    shard_id: Optional[int] = None
    total_shards: Optional[int] = None
    approximate_member_sum: Optional[int] = None
    ws_ping_ms: Optional[int] = None
    #: Process CPU usage, normalized to 0-100 across all cores. Filled in
    #: automatically when omitted; set to suppress the auto-measurement.
    cpu_percent: Optional[float] = None
    #: Process resident set size in megabytes. Filled in automatically when omitted.
    memory_mb: Optional[int] = None
    ts: Optional[str] = None

    def to_wire(self) -> dict[str, Any]:
        wire: dict[str, Any] = {"guildCount": self.guild_count}
        if self.shard_id is not None:
            wire["shardId"] = self.shard_id
        if self.total_shards is not None:
            wire["totalShards"] = self.total_shards
        if self.approximate_member_sum is not None:
            wire["approximateMemberSum"] = self.approximate_member_sum
        if self.ws_ping_ms is not None:
            wire["wsPingMs"] = self.ws_ping_ms
        if self.cpu_percent is not None:
            wire["cpuPercent"] = self.cpu_percent
        if self.memory_mb is not None:
            wire["memoryMb"] = self.memory_mb
        if self.ts is not None:
            wire["ts"] = self.ts
        return wire


class MochiClient:
    """Batching, non-blocking analytics client.

    ``track`` is synchronous and returns immediately - events are queued and
    flushed by a background asyncio task (every ``flush_interval`` seconds or
    whenever the batch fills). Transient failures retry with exponential
    backoff; the queue is bounded so a dead Mochi instance can never leak
    memory or crash the bot.

    The background flush loop starts lazily the first time ``track`` /
    ``snapshot`` / ``flush`` is called while an event loop is running, so a
    client can be constructed outside of a running loop.
    """

    def __init__(
        self,
        url: str,
        api_key: str,
        *,
        flush_interval: float = 5.0,
        max_batch_size: int = 100,
        max_queue_size: int = 10_000,
        max_retries: int = 3,
        on_error: Optional[Callable[[Exception], None]] = None,
        transport: Optional[Transport] = None,
    ) -> None:
        base = url.rstrip("/")
        self._ingest_url = f"{base}/api/v1/ingest"
        self._snapshot_url = f"{base}/api/v1/snapshot"
        self._api_key = api_key
        self._flush_interval = flush_interval
        self._max_batch_size = min(max_batch_size, 100)
        self._max_queue_size = max_queue_size
        self._max_retries = max_retries
        self._on_error = on_error or (lambda _err: None)
        self._transport = transport or self._default_transport

        self._queue: list[dict[str, Any]] = []
        self._flush_loop_task: Optional[asyncio.Task[None]] = None
        self._flushing: Optional[asyncio.Task[None]] = None
        self._shutdown = False
        self._session: Optional[Any] = None
        # CPU baseline for the delta between snapshots. CPU is a rate, so the
        # first snapshot only records memory and seeds these.
        self._last_cpu: float = 0.0
        self._last_cpu_at: Optional[float] = None

    # -- public API -----------------------------------------------------

    def track(self, event: MochiEvent) -> None:
        """Queue an event. Returns immediately; sending happens in the background."""
        if self._shutdown:
            return
        self._queue.append(event.to_wire())
        overflow = len(self._queue) - self._max_queue_size
        if overflow > 0:
            del self._queue[:overflow]
            self._report(MochiError("mochi: event queue overflow, dropped oldest"))
        self._ensure_loop()
        if len(self._queue) >= self._max_batch_size:
            self._schedule_flush()

    def track_command(self, name: str, **context: Any) -> None:
        """Convenience wrapper for a ``command`` event."""
        self.track(MochiEvent(type="command", name=name, **context))

    async def snapshot(self, snapshot: MochiSnapshot) -> None:
        """Send a guild-count / health snapshot immediately (with retries).

        Process CPU and memory are measured and attached automatically; values
        explicitly set on ``snapshot`` take precedence.
        """
        wire = snapshot.to_wire()
        for key, value in self._collect_resources().items():
            wire.setdefault(key, value)  # caller-provided values win
        try:
            await self._send(self._snapshot_url, wire)
        except Exception as error:  # never propagate into the caller
            self._report(error)

    def _collect_resources(self) -> dict[str, Any]:
        """Sample process CPU (0-100 across all cores, since the last snapshot)
        and resident memory. Best-effort; failures yield an empty dict."""
        out: dict[str, Any] = {}
        rss = _read_rss_mb()
        if rss is not None:
            out["memoryMb"] = rss
        try:
            now = time.monotonic()
            cpu = time.process_time()  # cumulative user+system CPU seconds
            if self._last_cpu_at is not None and now > self._last_cpu_at:
                cores = os.cpu_count() or 1
                pct = (cpu - self._last_cpu) / (now - self._last_cpu_at) / cores * 100
                out["cpuPercent"] = max(0.0, round(pct, 1))
            self._last_cpu = cpu
            self._last_cpu_at = now
        except Exception:
            pass
        return out

    async def flush(self) -> None:
        """Drain the queue. Safe to call concurrently; flushes are serialized."""
        if self._flushing and not self._flushing.done():
            await self._flushing
            return
        self._flushing = asyncio.ensure_future(self._drain())
        try:
            await self._flushing
        finally:
            self._flushing = None

    async def shutdown(self) -> None:
        """Stop the background loop and flush remaining events. Call on exit."""
        self._shutdown = True
        if self._flush_loop_task:
            self._flush_loop_task.cancel()
            try:
                await self._flush_loop_task
            except asyncio.CancelledError:
                pass
            self._flush_loop_task = None
        await self.flush()
        if self._session is not None:
            await self._session.close()
            self._session = None

    # -- internals ------------------------------------------------------

    def _ensure_loop(self) -> None:
        if self._flush_loop_task is not None or self._shutdown:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return  # no loop yet; loop starts on the next call inside one
        self._flush_loop_task = loop.create_task(self._flush_loop())

    async def _flush_loop(self) -> None:
        while not self._shutdown:
            await asyncio.sleep(self._flush_interval)
            await self.flush()

    def _schedule_flush(self) -> None:
        if self._flushing and not self._flushing.done():
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._flushing = loop.create_task(self._drain())

    async def _drain(self) -> None:
        while self._queue:
            batch = self._queue[: self._max_batch_size]
            del self._queue[: self._max_batch_size]
            try:
                await self._send(self._ingest_url, {"events": batch})
            except Exception as error:
                # Batch is dropped; don't spin on a failing endpoint.
                self._report(error)
                return

    async def _send(self, url: str, body: Any) -> None:
        last_error: Optional[Exception] = None
        retry_after: Optional[float] = None  # seconds, from a prior 429
        for attempt in range(self._max_retries + 1):
            if attempt > 0:
                # Honor a server-provided Retry-After in preference to the
                # computed backoff; otherwise use exponential backoff.
                delay = retry_after if retry_after is not None else 0.5 * 2 ** (attempt - 1)
                retry_after = None
                await asyncio.sleep(delay)
            try:
                # Transport returns (status, text) or (status, text, headers).
                status, text, *rest = await self._transport(url, body)
            except Exception as error:  # network error -> retry
                last_error = error
                continue
            if 200 <= status < 300:
                return
            if status not in RETRYABLE_STATUS:
                raise MochiError(f"mochi: request rejected ({status}) {text}")
            if status == 429 and rest:
                retry_after = _parse_retry_after(_header(rest[0], "Retry-After"))
            last_error = MochiError(f"mochi: server returned {status}")
        raise last_error or MochiError("mochi: request failed")

    async def _default_transport(self, url: str, body: Any) -> "tuple[int, str]":
        if aiohttp is None:  # pragma: no cover
            raise MochiError(
                "mochi: aiohttp is not installed; install mochi-analytics with its "
                "default dependencies or pass a custom transport"
            )
        session = await self._get_session()
        async with session.post(
            url,
            json=body,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self._api_key}",
            },
        ) as response:
            text = await response.text()
            # response.headers is a case-insensitive CIMultiDict.
            return response.status, text, response.headers

    async def _get_session(self) -> Any:
        if self._session is None:
            self._session = aiohttp.ClientSession()
        return self._session

    def _report(self, error: Exception) -> None:
        try:
            self._on_error(error)
        except Exception:  # an error handler must never take down the bot
            pass
