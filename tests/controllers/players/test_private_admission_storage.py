"""Offline player configuration retention checks for private admission."""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock

from music_assistant_models.config_entries import ProviderConfig
from music_assistant_models.enums import ProviderType
from music_assistant_models.provider import ProviderManifest

from music_assistant.controllers.players import PlayerController
from music_assistant.mass import MusicAssistant
from music_assistant.models.player import Player
from music_assistant.models.player_provider import PlayerProvider

if TYPE_CHECKING:
    import pytest


async def test_nonpermanent_unregister_retains_created_config(
    mass_minimal: MusicAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A real player registration and temporary unregister retain the persisted record."""
    mass = mass_minimal
    mass.players = PlayerController(mass)
    mass.player_queues = MagicMock(on_player_register=AsyncMock(), get=MagicMock(return_value=None))
    monkeypatch.setattr(mass.cache, "get", AsyncMock(return_value=False))
    monkeypatch.setattr(
        "music_assistant.controllers.players.controller.enrich_device_mac_address", AsyncMock()
    )
    cfg = ProviderConfig(
        values={},
        type=ProviderType.PLAYER,
        domain="test_player_provider",
        instance_id="test_player_provider--instance",
        name="Test player provider",
    )
    monkeypatch.setattr(cfg, "get_value", lambda *_args, **_kwargs: "GLOBAL")
    provider = PlayerProvider(
        mass,
        manifest=ProviderManifest(
            type=ProviderType.PLAYER,
            domain="test_player_provider",
            name="Test player provider",
            description="Test player provider",
            codeowners=["@music-assistant"],
        ),
        config=cfg,
    )
    provider.available = True
    mass._providers[provider.instance_id] = provider
    player = Player(provider, "apaabbccddeeff")
    created = deepcopy(mass.config.get("players/apaabbccddeeff"))
    assert created
    await mass.config.async_save()
    path = Path(mass.config.filename)
    assert json.loads(path.read_text())["players"][player.player_id] == created
    await mass.players.register(player)
    assert mass.players.get_player(player.player_id) is player
    await mass.config.async_save()
    assert json.loads(path.read_text())["players"][player.player_id] == mass.config.get(
        f"players/{player.player_id}"
    )
    before = deepcopy(mass.config.get(f"players/{player.player_id}"))
    await mass.players.unregister(player.player_id, permanent=False)
    assert mass.players.get_player(player.player_id) is None
    assert mass.config.get(f"players/{player.player_id}") == before
    await mass.config.async_save()
    assert json.loads(path.read_text())["players"][player.player_id] == before
