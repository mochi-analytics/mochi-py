# mochi-analytics

Core client for Mochi — self-hosted analytics for Discord bots. Asyncio-native,
batching, and non-blocking: `track()` never blocks or raises into your bot.

## Install

```sh
pip install mochi-analytics
```

## Usage

```python
from mochi_analytics import MochiClient, MochiEvent, MochiSnapshot

mochi = MochiClient(url="https://mochi.example.com", api_key="mochi_sk_...")

# Queue events — sent in the background, batched.
mochi.track(MochiEvent(type="command", name="play", user_id="123"))
mochi.track_command("play", guild_id="456")

# Send a health snapshot immediately.
await mochi.snapshot(MochiSnapshot(guild_count=1204, ws_ping_ms=38))

# On shutdown, flush anything still queued.
await mochi.shutdown()
```

## Design guarantees

- Events are batched (flushed every 5 s or 100 events) and sent in the
  background — `track()` never blocks or raises.
- Transient failures (429 / 5xx / network) retry with exponential backoff;
  the queue is bounded (oldest dropped first), so a dead Mochi instance can
  never leak memory or crash the bot.
- Errors are routed to the optional `on_error` callback, never raised.

## Options

| Argument | Default | Notes |
|---|---|---|
| `flush_interval` | `5.0` | Seconds between background flushes. |
| `max_batch_size` | `100` | Events per request (server limit is 100). |
| `max_queue_size` | `10000` | Events beyond this are dropped oldest-first. |
| `max_retries` | `3` | Retry attempts for retryable failures. |
| `on_error` | no-op | `Callable[[Exception], None]` for dropped/failed sends. |
| `transport` | aiohttp | Injectable `async (url, body) -> (status, text)` for testing. |

## License

Apache-2.0
