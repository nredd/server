"""AirPlay Player provider for Music Assistant."""

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ProviderConfig
    from music_assistant_models.provider import ProviderManifest

    from music_assistant.mass import MusicAssistant
    from music_assistant.models import ProviderInstanceType


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Initialize provider(instance) with given configuration."""
    provider_module = importlib.import_module("music_assistant.providers.airplay.provider")
    model_enums = importlib.import_module("music_assistant_models.enums")
    supported_features = {model_enums.ProviderFeature.SYNC_PLAYERS}
    return cast(
        "ProviderInstanceType",
        provider_module.AirPlayProvider(mass, manifest, config, supported_features),
    )
