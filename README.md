<p align="center">
  <img src="assets/logo.png" alt="" width="96" height="96">
</p>

# Mochi Python SDK

Python SDK packages for [Mochi](https://github.com/mochi-analytics/mochi), self-hosted analytics for Discord bots.

## Packages

- `mochi-analytics` — asyncio-native batching HTTP client for Mochi ingest and snapshot APIs
- `mochi-analytics-discordpy` — discord.py v2 adapter for command, guild, and health instrumentation

Future Python Discord libraries should be added under `packages/` and depend on `mochi-analytics`.

## Install

```sh
pip install mochi-analytics mochi-analytics-discordpy
```

```python
import discord
from mochi_analytics import MochiClient
from mochi_analytics_discordpy import attach_mochi

mochi = MochiClient(
    url="https://mochi.example.com",
    api_key=os.environ["MOCHI_API_KEY"],
)

client = discord.Client(intents=discord.Intents.default())
attach_mochi(client, mochi)
```

See [docs/sdk-discordpy.md](./docs/sdk-discordpy.md) for the full discord.py guide.

## Development

Each package is an independent, pip-installable project under `packages/`.

```sh
pip install -e "packages/core[dev]" -e "packages/discordpy[dev]"
pytest packages/core packages/discordpy
```

## Releases

Publishing is handled by the `Publish` GitHub Actions workflow using PyPI
[trusted publishing](https://docs.pypi.org/trusted-publishers/) with GitHub
Actions OIDC — no long-lived API tokens. Configure each PyPI project with this
trusted publisher:

- Owner: `mochi-analytics`
- Repository: `mochi-py`
- Workflow filename: `publish.yml`

## License

Apache-2.0
