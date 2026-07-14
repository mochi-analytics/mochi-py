# mochi-analytics-interactions

interactions.py adapter for Mochi analytics.

## Install

```sh
pip install mochi-analytics mochi-analytics-interactions discord-py-interactions
```

## Usage

```python
import interactions
from mochi_analytics import MochiClient
from mochi_analytics_interactions import attach_mochi

mochi = MochiClient(url="https://mochi.example.com", api_key="mochi_sk_...")

client = interactions.Client()
detach = attach_mochi(client, mochi)

client.start("...")
```

Commands are recorded on `CommandCompletion`, which interactions.py dispatches
in a `finally` — so it fires for failures too, and a preceding `CommandError` is
what marks an invocation unsuccessful. Neither event reports how long the
command took, so `durationMs` is omitted unless you pass
`auto_track_commands=False` and wrap your callbacks with `wrap_command`.

An `AutoShardedClient` sends one snapshot per shard, each carrying that shard's
own guild count.

See the [interactions.py guide](https://mochi.software/sdks/interactions) for the full documentation.

## Community

Questions? Join the [Mochi Discord](https://discord.gg/59z89Ke4bt).

## License

Apache-2.0
