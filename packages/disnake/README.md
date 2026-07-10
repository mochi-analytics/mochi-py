# mochi-analytics-disnake

disnake adapter for Mochi analytics.

## Install

```sh
pip install mochi-analytics mochi-analytics-disnake disnake
```

## Usage

```python
import disnake
from mochi_analytics import MochiClient
from mochi_analytics_disnake import attach_mochi

mochi = MochiClient(url="https://mochi.example.com", api_key="mochi_sk_...")

client = disnake.Client(intents=disnake.Intents.default())
detach = attach_mochi(client, mochi)
```

An `AutoShardedClient` sends one snapshot per shard, each carrying that shard's
own guild count.

See the [disnake guide](https://docs.mochis.dev/sdks/disnake) for the full documentation.

## License

Apache-2.0
