"""Controller regression tests for automatic power-on policy."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from music_assistant_models.constants import PLAYER_CONTROL_FAKE, PLAYER_CONTROL_NATIVE
from music_assistant_models.enums import MediaType, PlayerFeature
from music_assistant_models.player import PlayerMedia

from music_assistant.constants import CONF_POWER_CONTROL
from music_assistant.controllers.players import PlayerController
from tests.controllers.players.test_player_controller import _player_config_stub
from tests.controllers.players.test_player_controller import mock_mass as controller_mock_mass
from tests.providers.airplay.test_control_player import _make_control_player
from tests.providers.universal_group.test_universal_group import _make_ugp

mock_mass = controller_mock_mass


@pytest.mark.parametrize("command", ["play_media", "cmd_play", "cmd_resume"])
@pytest.mark.parametrize(
    ("trial", "model", "expect_power"),
    [(True, "Apple TV 4K", False), (False, "Apple TV 4K", True), (True, "HomePod", True)],
)
async def test_controller_playback_auto_power(
    mock_mass: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
    command: str,
    trial: bool,
    model: str,
    expect_power: bool,
) -> None:
    """Playback reaches the controller while a private TV stays truthfully off."""
    monkeypatch.setattr(
        "music_assistant.providers.airplay.control_player.trial_binary.is_trial_target",
        lambda: trial,
    )
    mock_mass.config.get_raw_player_config_value.side_effect = _player_config_stub(
        {CONF_POWER_CONTROL: PLAYER_CONTROL_NATIVE}
    )
    controller = PlayerController(mock_mass)
    player = _make_control_player(model=model, setup_data={"companion_credentials": "credentials"})
    player.provider.mass = mock_mass
    player._attr_powered = False
    player._attr_supported_features.add(PlayerFeature.POWER)
    controller._players = {player.player_id: player}
    mock_mass.players = controller
    mock_mass.player_queues.get.return_value = None
    mock_mass.player_queues.resume = AsyncMock()
    player.set_initialized()
    player.update_state(signal_event=False)
    assert player.state.powered is False
    assert player.state.power_control == PLAYER_CONTROL_NATIVE
    media = PlayerMedia(uri="http://example.test/audio", media_type=MediaType.ANNOUNCEMENT)
    power = AsyncMock()
    with (
        patch.object(player, "power_for_playback", power),
        patch.object(player, "play_media", new=AsyncMock()),
        patch.object(player, "play", new=AsyncMock()),
        patch("music_assistant.controllers.players.controller.wait_for_power_on", new=AsyncMock()),
    ):
        if command == "play_media":
            await controller.play_media(player.player_id, media)
        elif command == "cmd_play":
            await controller.cmd_play(player.player_id)
        else:
            await controller.cmd_resume(player.player_id, media=media)
    assert power.await_count == int(expect_power)
    assert player.state.powered is False


async def test_controller_explicit_power_still_reaches_tv(
    mock_mass: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The private policy does not suppress an explicit power command."""
    monkeypatch.setattr(
        "music_assistant.providers.airplay.control_player.trial_binary.is_trial_target",
        lambda: True,
    )
    mock_mass.config.get_raw_player_config_value.side_effect = _player_config_stub(
        {CONF_POWER_CONTROL: PLAYER_CONTROL_NATIVE}
    )
    controller = PlayerController(mock_mass)
    player = _make_control_player(setup_data={"companion_credentials": "credentials"})
    player.provider.mass = mock_mass
    player._attr_powered = False
    player._attr_supported_features.add(PlayerFeature.POWER)
    controller._players = {player.player_id: player}
    mock_mass.players = controller
    player.set_initialized()
    player.update_state(signal_event=False)
    with (
        patch.object(player, "power", new=AsyncMock()) as power,
        patch("music_assistant.controllers.players.controller.wait_for_power_on", new=AsyncMock()),
    ):
        await controller.cmd_power(player.player_id, True)
    power.assert_awaited_once_with(True)


@pytest.mark.parametrize("automatic", [False, True])
async def test_controller_group_power_provenance(
    mock_mass: MagicMock, monkeypatch: pytest.MonkeyPatch, automatic: bool
) -> None:
    """Explicit group power wakes the TV, while automatic group power does not."""
    monkeypatch.setattr(
        "music_assistant.providers.airplay.control_player.trial_binary.is_trial_target",
        lambda: True,
    )
    config_value = _player_config_stub({CONF_POWER_CONTROL: PLAYER_CONTROL_NATIVE})
    mock_mass.config.get_raw_player_config_value.side_effect = lambda player_id, key, default=None: (
        PLAYER_CONTROL_FAKE
        if player_id == "ugp_test" and key == CONF_POWER_CONTROL
        else config_value(player_id, key, default)
    )
    controller = PlayerController(mock_mass)
    mock_mass.players = controller
    group = _make_ugp(mock_mass)
    player = _make_control_player(setup_data={"companion_credentials": "credentials"})
    player.provider.mass = mock_mass
    player._attr_powered = False
    controller._players = {group.player_id: group, player.player_id: player}
    group._attr_powered = False
    mock_mass.player_queues.resume = AsyncMock()
    for item in (group, player):
        item.set_initialized()
        item.update_state(signal_event=False)
    with (
        patch.object(
            controller, "iter_group_members", side_effect=lambda *_args, **_kwargs: iter([player])
        ),
        patch.object(player, "power", new=AsyncMock()) as power,
        patch("music_assistant.controllers.players.controller.wait_for_power_on", new=AsyncMock()),
    ):
        if automatic:
            await controller.cmd_power_for_playback(group.player_id)
        else:
            await controller.cmd_power(group.player_id, True)
    assert power.await_count == int(not automatic)
