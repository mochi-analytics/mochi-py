# mochi-analytics-nextcord

nextcord adapter for Mochi analytics.

## Install

```sh
pip install mochi-analytics mochi-analytics-nextcord nextcord
```

## Usage

```python
import nextcord
from mochi_analytics import MochiClient
from mochi_analytics_nextcord import attach_mochi

mochi = MochiClient(url="https://mochi.example.com", api_key="mochi_sk_...")

client = nextcord.Client(intents=nextcord.Intents.default())
detach = attach_mochi(client, mochi)
```

An `AutoShardedClient` sends one snapshot per shard, each carrying that shard's
own guild count.

See the [nextcord guide](https://docs.mochis.dev/sdks/nextcord) for the full documentation.

## License

Apache-2.0
