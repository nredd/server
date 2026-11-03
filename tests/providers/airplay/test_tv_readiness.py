"""
Offline Apple-TV admission and unpaired sender regression coverage.

References:
    https://github.com/nredd/server
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from music_assistant.helpers.private_trial import admits_mdns_name, parse_trial_target
from music_assistant.providers.airplay import trial_binary
from music_assistant.providers.airplay.stream import AirPlayStream
from tests.providers.airplay.test_control_player import _make_control_player
from tests.providers.airplay.test_private_capture_api import _child
from tests.providers.airplay.test_private_target_admission import _info, _provider
from tests.providers.airplay.test_stream import _make_player

TARGET = "112233445566@Synthetic TV"
PLAYER_ID = "ap112233445566"


async def test_tv_target_admission(monkeypatch: pytest.MonkeyPatch) -> None:
    """A TV-shaped record reaches ordinary setup only for the selected target."""
    target = parse_trial_target(TARGET)
    assert target is not None
    assert admits_mdns_name(
        target, "112233445566@Synthetic TV._raop._tcp.local.", "_raop._tcp.local."
    )
    prov = _provider(monkeypatch, TARGET)
    receiver = AsyncMock(return_value=True)
    monkeypatch.setattr(prov, "_is_own_airplay_receiver", receiver)
    info = _info()
    info.decoded_properties = {"am": "AppleTV14,1", "et": "0,3,5", "cn": "0,1,2,3"}
    await prov._setup_player(PLAYER_ID, "Synthetic TV", info)
    receiver.assert_awaited_once()
    receiver.reset_mock()
    await prov._setup_player("apaabbccddeeff", "Other", info)
    receiver.assert_not_awaited()


@pytest.mark.parametrize("companion_flags", [None, "0x367A2"])
async def test_unpaired_tv_without_native_mrp_never_connects(companion_flags: str | None) -> None:
    """An unpaired TV without native MRP does not connect via Companion or tunnel."""
    player = _make_control_player(companion_flags=companion_flags, setup_data={})
    assert player.mrp_discovery_info is None
    assert player.airplay_discovery_info is not None
    player.airplay_discovery_info.decoded_properties["model"] = "AppleTV14,1"
    with patch(
        "music_assistant.providers.airplay.control_player.pyatv.connect",
        new_callable=AsyncMock,
    ) as connect:
        assert player._uses_transient_mrp is False
        assert await player._connect_companion() is False
        assert await player._connect_mrp() is False
    connect.assert_not_called()


async def test_tv_default_stream_policy(monkeypatch: pytest.MonkeyPatch) -> None:
    """Actual argv and process env preserve TV auto routing with experiments off."""
    player = _make_player()
    player.device_info.model = "AppleTV14,1"
    player.device_info.manufacturer = "Apple"
    player.airplay_discovery_info.decoded_properties = {
        "model": "AppleTV14,1",
        "features": "0x4A7FDFD5,0x3C177FDE",
        "flags": "0x18644",
    }
    player.raop_discovery_info.decoded_properties = {
        "am": "AppleTV14,1",
        "et": "0,3,5",
        "cn": "0,1,2,3",
        "md": "0,1,2",
    }
    player.provider.config.get_value.side_effect = lambda _key, default=None: default
    player.provider.mass.create_task.side_effect = lambda coro, **_kwargs: (
        coro.close(),
        MagicMock(),
    )[1]
    stream = AirPlayStream(player)
    child = _child()
    monkeypatch.setattr(child, "start", AsyncMock())
    factory = AsyncMock(return_value=child)
    monkeypatch.setattr(trial_binary, "is_trial_target", lambda: True)
    monkeypatch.setattr(trial_binary, "create_private_process", factory)
    monkeypatch.setattr(stream.commands_pipe, "create", AsyncMock())
    with patch(
        "music_assistant.providers.airplay.stream.get_cli_binary",
        new=AsyncMock(return_value="/fake/cli"),
    ):
        await stream.connect()
    args = factory.call_args.args[0]
    for key, value in {
        "--protocol": "auto",
        "--et": "0,3,5",
        "--cn": "0,1,2,3",
        "--am": "AppleTV14,1",
    }.items():
        assert args[args.index(key) + 1] == value
    env = factory.call_args.kwargs["env"]
    assert env["CLIAIRPLAY_EXPERIMENTAL_HOMEPOD_REMOTE"] == "0"
    assert env["CLIAIRPLAY_SESSION_VOLUME"] == "off"
    assert env["CLIAIRPLAY_NATIVE_VOLUME_RAIL"] == "0"
    assert env["CLIAIRPLAY_PRIVATE_NATIVE_VOLUME_CAPTURE"] == "0"
