# discord.py SDK

```sh
pip install mochi-analytics mochi-analytics-discordpy
```

## Quick start

```python
import os
import discord
from mochi_analytics import MochiClient
from mochi_analytics_discordpy import attach_mochi

mochi = MochiClient(
    url="https://mochi.example.com",       # your Mochi instance
    api_key=os.environ["MOCHI_API_KEY"],   # from the bot's settings page
)

intents = discord.Intents.default()
client = discord.Client(intents=intents)
attach_mochi(client, mochi)

@client.event
async def on_close():
    await mochi.shutdown()  # flush remaining events

client.run(os.environ["DISCORD_TOKEN"])
```

That's it. Mochi now records slash/context-menu command usage, guild joins
and leaves, and an hourly server-count snapshot.

> `attach_mochi` works with `discord.Client`, `discord.AutoShardedClient`, and
> `discord.ext.commands.Bot`.

## Options

```python
attach_mochi(
    client,
    mochi,
    include_guild_names=True,   # put guild names in join/leave metadata
    ignore_commands=["ping"],   # skip noisy commands
    snapshot_interval=30 * 60,  # seconds; default 1 hour
    auto_track_commands=False,  # see "accurate timings" below
)
```

## Accurate duration & success

Auto-tracking records commands the moment the interaction arrives — it can't
see whether your handler succeeded or how long it took. For that, disable
auto-tracking and wrap your handlers:

```python
from discord import app_commands
from mochi_analytics_discordpy import wrap_command

attach_mochi(client, mochi, auto_track_commands=False)

tree = app_commands.CommandTree(client)

@tree.command()
@wrap_command(mochi)
async def play(interaction: discord.Interaction):
    ...  # duration and raised exceptions are recorded
```

## Custom events

```python
from mochi_analytics import MochiEvent

mochi.track(MochiEvent(
    type="custom",
    name="premium_purchased",
    user_id=str(interaction.user.id),
    guild_id=str(interaction.guild_id) if interaction.guild_id else None,
    meta={"tier": "gold"},
))
```

## Design guarantees

- Events are batched (flushed every 5 s or 100 events) and sent in the
  background — `track()` never blocks or raises.
- Transient failures retry with backoff; the queue is bounded (oldest
  dropped first), so a dead Mochi instance can never leak memory or crash
  the bot.
- Raw user ids are hashed server-side with a per-bot salt and never stored.

## Other libraries / languages

Everything above is a thin wrapper over two HTTP endpoints — see
[ingest-api.md](https://github.com/mochi-analytics/mochi/blob/main/docs/ingest-api.md)
to integrate from any other framework.
