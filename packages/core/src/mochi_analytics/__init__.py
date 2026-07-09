"""Core client for Mochi - self-hosted analytics for Discord bots."""

from mochi_analytics.client import (
    MochiChannelType,
    MochiClient,
    MochiError,
    MochiEvent,
    MochiEventType,
    MochiSnapshot,
    Transport,
)

__all__ = [
    "MochiChannelType",
    "MochiClient",
    "MochiError",
    "MochiEvent",
    "MochiEventType",
    "MochiSnapshot",
    "Transport",
]

__version__ = "1.0.0"  # x-release-please-version
