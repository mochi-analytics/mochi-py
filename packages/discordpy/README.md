# mochi-analytics-discordpy

discord.py v2 adapter for Mochi analytics.

## Install

```sh
pip install mochi-analytics mochi-analytics-discordpy discord.py
```

## Usage

```python
import discord
from mochi_analytics import MochiClient
from mochi_analytics_discordpy import attach_mochi

mochi = MochiClient(url="https://mochi.example.com", api_key="mochi_sk_...")

client = discord.Client(intents=discord.Intents.default())
detach = attach_mochi(client, mochi)
```

See the [discord.py guide](https://docs.mochis.dev/sdks/discordpy) for the full documentation.

## License

Apache-2.0
