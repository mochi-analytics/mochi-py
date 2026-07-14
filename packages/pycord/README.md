# mochi-analytics-pycord

Py-cord adapter for Mochi analytics.

> Py-cord installs itself under the `discord` import name, exactly as discord.py
> does, so the two cannot coexist in one environment. Install
> `mochi-analytics-pycord` **or** `mochi-analytics-discordpy`, never both.

## Install

```sh
pip install mochi-analytics mochi-analytics-pycord py-cord
```

## Usage

```python
import discord
from mochi_analytics import MochiClient
from mochi_analytics_pycord import attach_mochi

mochi = MochiClient(url="https://mochi.example.com", api_key="mochi_sk_...")

bot = discord.Bot(intents=discord.Intents.default())
detach = attach_mochi(bot, mochi)
```

An `AutoShardedClient` sends one snapshot per shard, each carrying that shard's
own guild count.

See the [Py-cord guide](https://mochi.software/sdks/pycord) for the full documentation.

## Community

Questions? Join the [Mochi Discord](https://discord.gg/59z89Ke4bt).

## License

Apache-2.0
