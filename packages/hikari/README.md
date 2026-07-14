# mochi-analytics-hikari

hikari adapter for Mochi analytics.

## Install

```sh
pip install mochi-analytics mochi-analytics-hikari hikari
```

## Usage

```python
import hikari
from mochi_analytics import MochiClient
from mochi_analytics_hikari import attach_mochi

mochi = MochiClient(url="https://mochi.example.com", api_key="mochi_sk_...")

bot = hikari.GatewayBot(token="...")
detach = attach_mochi(bot, mochi)

bot.run()
```

hikari runs every shard inside one process, so one snapshot is sent per shard,
each carrying that shard's own guild count.

hikari has no built-in command framework, so `attach_mochi` records commands
straight off `InteractionCreateEvent`. For accurate `success` and `duration`,
pass `auto_track_commands=False` and wrap your handlers with `wrap_command`.

See the [hikari guide](https://mochi.software/sdks/hikari) for the full documentation.

## Community

Questions? Join the [Mochi Discord](https://discord.gg/59z89Ke4bt).

## License

Apache-2.0
