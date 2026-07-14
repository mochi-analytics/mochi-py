<p align="center">
  <img src="assets/logo.png" alt="" width="96" height="96">
</p>

# Mochi Python SDK

Python SDK packages for [Mochi](https://github.com/mochi-analytics/mochi), self-hosted analytics for Discord bots.

## Packages

- `mochi-analytics` — asyncio-native batching HTTP client for Mochi ingest and snapshot APIs
- `mochi-analytics-discordpy` — discord.py v2 adapter for command, guild, and health instrumentation
- `mochi-analytics-nextcord` — nextcord adapter
- `mochi-analytics-disnake` — disnake adapter
- `mochi-analytics-pycord` — Py-cord adapter
- `mochi-analytics-hikari` — hikari adapter
- `mochi-analytics-interactions` — interactions.py adapter

Every adapter exposes the same `attach_mochi(client, mochi, **options)` returning
a `detach` callable. Future Python Discord libraries should be added under
`packages/` and depend on `mochi-analytics`.

> Py-cord and discord.py both install themselves under the `discord` import name,
> so `mochi-analytics-pycord` and `mochi-analytics-discordpy` cannot share an
> environment. Install one or the other.

The adapters other than `discordpy` require Python 3.10+, which is the floor
their underlying libraries set.

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

Full guides live at [mochi.software/sdks](https://mochi.software/sdks), one per
library — [discord.py](https://mochi.software/sdks/discordpy),
[nextcord](https://mochi.software/sdks/nextcord),
[disnake](https://mochi.software/sdks/disnake),
[Py-cord](https://mochi.software/sdks/pycord),
[hikari](https://mochi.software/sdks/hikari), and
[interactions.py](https://mochi.software/sdks/interactions). They are maintained
in the [mochi-docs](https://github.com/mochi-analytics/mochi-docs) repo, which is
the single source of truth for documentation.

## Development

Each package is an independent, pip-installable project under `packages/`.

```sh
pip install -e "packages/core[dev]" -e "packages/discordpy[dev]"
pytest packages/core packages/discordpy
```

The other adapters install alongside each other, but `pycord` needs its own
environment because it collides with `discordpy` on the `discord` module:

```sh
pip install -e "packages/core[dev]" -e "packages/nextcord[dev]" \
  -e "packages/disnake[dev]" -e "packages/hikari[dev]" -e "packages/interactions[dev]"
pytest packages/core packages/nextcord packages/disnake packages/hikari packages/interactions
```

## Releases

Publishing is handled by the `Publish` GitHub Actions workflow using PyPI
[trusted publishing](https://docs.pypi.org/trusted-publishers/) with GitHub
Actions OIDC — no long-lived API tokens. Configure each PyPI project with this
trusted publisher:

- Owner: `mochi-analytics`
- Repository: `mochi-py`
- Workflow filename: `publish.yml`

## Community

Questions or want to share what you built? Join the
[Mochi Discord](https://discord.gg/59z89Ke4bt).

## License

Apache-2.0
