"""Offline bootstrap and installer provenance regressions for the private overlay."""

from __future__ import annotations

import asyncio
import importlib.metadata
import json
import threading
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import music_assistant
from music_assistant import __main__ as entrypoint
from music_assistant.controllers.config import ConfigController
from music_assistant.helpers import private_trial as trial
from music_assistant.mass import MusicAssistant


@pytest.fixture
def app_data(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Use a disposable app-owned data root, never actual original app data."""
    root = tmp_path / "data"
    root.mkdir()
    monkeypatch.setattr(trial, "APP_DATA", root)
    monkeypatch.setattr(trial, "is_trial_target", lambda: True)
    monkeypatch.setattr(trial, "_FRESH_ROOTS", set())
    return root


def _prepare(root: Path) -> tuple[str, str]:
    """Use the fixed official entrypoint contract with a test-only root."""
    return trial.prepare_data(str(root), str(root / ".cache"))


async def test_fresh_config_and_validated_restart(app_data: Path) -> None:
    """Use the real config UUID generator, preserve original state, reject a changed ID."""
    original = app_data / "settings.json"
    original.write_text('{"server_id":"must-not-load","secret":"never-copy"}')
    before = original.read_bytes()
    data, cache = _prepare(app_data)
    assert list(Path(data).iterdir()) == [Path(cache)]
    assert not list(Path(cache).iterdir())
    trial.validate_bootstrap(data, cache)
    mass = MusicAssistant(data, cache)
    mass.loop = asyncio.get_running_loop()
    mass.loop_thread_id = threading.get_ident()
    mass.config = ConfigController(mass)
    await mass.config.setup()
    try:
        await mass.config.async_save()
        trial.bind_server_id(data, mass.server_id)
        assert len(mass.server_id) == 32
        assert _prepare(app_data) == (data, cache)
        trial.validate_bootstrap(data, cache)
        trial.bind_server_id(data, mass.server_id)
        assert original.read_bytes() == before
        assert b"never-copy" not in Path(data, "settings.json").read_bytes()
        marker = app_data / trial.NAMESPACE / trial.MARKER
        marker.write_text(json.dumps({"version": trial.VERSION, "server_id": "0" * 32}))
        with pytest.raises(trial.PrivateTrialError, match="identity"):
            _prepare(app_data)
    finally:
        await mass.config.close()


@pytest.mark.parametrize("kind", ["empty", "seed", "symlink", "pending", "badversion"])
def test_unknown_namespace_never_adopted_or_erased(app_data: Path, kind: str) -> None:
    """Existing namespaces, including interrupted first boots, fail closed."""
    root = app_data / trial.NAMESPACE
    root.mkdir()
    if kind == "seed":
        (root / "settings.json").write_text("private seed")
    elif kind == "symlink":
        root.rmdir()
        root.symlink_to(app_data, target_is_directory=True)
    elif kind in ("pending", "badversion"):
        (root / "data" / ".cache").mkdir(parents=True)
        (root / trial.MARKER).write_text(
            json.dumps(
                {
                    "version": trial.VERSION if kind == "pending" else 9,
                    "server_id": None,
                }
            )
        )
    with pytest.raises(trial.PrivateTrialError):
        _prepare(app_data)
    assert root.exists()
    if kind == "seed":
        assert (root / "settings.json").read_text() == "private seed"


@pytest.mark.parametrize("kind", ["data", "cache", "symlink"])
def test_seed_between_prepare_and_bootstrap_rejected(app_data: Path, kind: str) -> None:
    """No configuration or cache seed can be accepted on first bootstrap."""
    data, cache = _prepare(app_data)
    if kind == "data":
        Path(data, "settings.json").write_text("{}")
    elif kind == "cache":
        Path(cache, "seed").write_text("seed")
    else:
        Path(data, "settings.json").symlink_to(app_data / "settings.json")
    with pytest.raises(trial.PrivateTrialError):
        trial.validate_bootstrap(data, cache)


@pytest.mark.parametrize("damage", ["version", "boolversion", "id", "settings", "symlink", "extra"])
def test_restart_rejects_changed_or_unexpected_state(app_data: Path, damage: str) -> None:
    """All startup routes require the trial's own valid marker and matching settings."""
    data, cache = _prepare(app_data)
    server_id = "b" * 32
    settings = Path(data, "settings.json")
    settings.write_text(json.dumps({"server_id": server_id}))
    trial.bind_server_id(data, server_id)
    marker = app_data / trial.NAMESPACE / trial.MARKER
    if damage in ("version", "boolversion", "id"):
        marker.write_text(
            json.dumps(
                {
                    "version": True
                    if damage == "boolversion"
                    else 2
                    if damage == "version"
                    else trial.VERSION,
                    "server_id": "short" if damage == "id" else server_id,
                }
            )
        )
    elif damage == "settings":
        settings.write_text(json.dumps({"server_id": "c" * 32}))
    elif damage == "symlink":
        settings.unlink()
        settings.symlink_to(app_data / "original-settings.json")
    else:
        (marker.parent / "unknown-seed").write_text("do-not-erase")
    with pytest.raises(trial.PrivateTrialError):
        _prepare(app_data)
    with pytest.raises(trial.PrivateTrialError):
        trial.validate_bootstrap(data, cache)
    if damage == "extra":
        assert (marker.parent / "unknown-seed").read_text() == "do-not-erase"


def test_entrypoint_isolates_before_options_and_logging(app_data: Path) -> None:
    """The actual entrypoint redirects before looking for official app options or logs."""
    options = app_data / "options.json"
    options.write_text("original-private-not-json")
    args = MagicMock(
        data_dir=str(app_data),
        cache_dir=str(app_data / ".cache"),
        log_level="info",
        safe_mode=False,
    )
    with (
        patch.object(entrypoint, "get_arguments", return_value=args),
        patch.object(entrypoint, "setup_logger") as logger,
        patch.object(entrypoint, "cap_native_thread_pools"),
        patch.object(entrypoint, "set_model_cache_dirs"),
        patch.object(
            entrypoint, "MusicAssistant", side_effect=RuntimeError("stop-before-start")
        ) as server,
        pytest.raises(RuntimeError, match="stop-before-start"),
    ):
        entrypoint.main()
    expected = str(app_data / trial.NAMESPACE / "data")
    logger.assert_called_once_with(expected, "INFO")
    server.assert_called_once_with(expected, str(Path(expected) / ".cache"), False)
    assert options.read_text() == "original-private-not-json"


async def test_real_start_binds_id_before_discovery(app_data: Path) -> None:
    """The actual start path persists and binds MA's UUID before creating discovery."""
    data, cache = _prepare(app_data)
    mass = MusicAssistant(data, cache)
    with (
        patch("music_assistant.mass.is_hass_supervisor", new=AsyncMock(return_value=False)),
        patch("music_assistant.mass.get_package_version", new=AsyncMock(return_value="test")),
        patch("music_assistant.mass.get_official_build_info", new=AsyncMock()),
        patch(
            "music_assistant.mass.DiscoveryController", side_effect=RuntimeError("before-discovery")
        ),
        pytest.raises(RuntimeError, match="before-discovery"),
    ):
        await mass.start()
    try:
        assert _prepare(app_data) == (data, cache)
        trial.validate_bootstrap(data, cache)
        trial.bind_server_id(data, mass.server_id)
    finally:
        await mass.config.close()


@pytest.mark.parametrize("invalid", ["NaN", "Infinity", "-Infinity", '"\\ud800"'])
async def test_parser_invalid_settings_cannot_reach_config_backup(
    app_data: Path, invalid: str
) -> None:
    """The real start path rejects parser-invalid settings before loading any backup."""
    data, cache = _prepare(app_data)
    server_id = "b" * 32
    settings = Path(data, "settings.json")
    settings.write_text(json.dumps({"server_id": server_id}))
    trial.bind_server_id(data, server_id)
    settings.write_text('{"server_id":"' + server_id + '","invalid":' + invalid + "}")
    backup = Path(data, "settings.json.backup")
    backup.write_text(json.dumps({"server_id": "c" * 32, "sentinel": "must-not-load"}))
    before = {path: path.read_bytes() for path in (settings, backup)}
    mass = MusicAssistant(data, cache)
    original_setup = ConfigController.setup
    try:
        with (
            patch.object(
                ConfigController, "setup", autospec=True, side_effect=original_setup
            ) as setup,
            patch("music_assistant.mass.is_hass_supervisor", new=AsyncMock(return_value=False)),
            patch("music_assistant.mass.get_package_version", new=AsyncMock(return_value="test")),
            patch("music_assistant.mass.get_official_build_info", new=AsyncMock()),
            pytest.raises(trial.PrivateTrialError),
        ):
            await mass.start()
        setup.assert_not_awaited()
        assert {path: path.read_bytes() for path in before} == before
        with pytest.raises(trial.PrivateTrialError):
            _prepare(app_data)
    finally:
        if hasattr(mass, "config"):
            await mass.config.close()


async def test_embedded_start_rejects_original_before_config(app_data: Path) -> None:
    """Direct starts cannot bypass the entrypoint's namespace selection."""
    mass = MusicAssistant(str(app_data), str(app_data / ".cache"))
    with patch.object(ConfigController, "setup", new_callable=AsyncMock) as setup:
        with pytest.raises(trial.PrivateTrialError, match="not prepared"):
            await mass.start()
        setup.assert_not_awaited()


def test_nontrial_paths_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    """Other platforms keep the ordinary data and cache contract."""
    monkeypatch.setattr(trial, "is_trial_target", lambda: False)
    assert trial.prepare_data("/original", "/cache") == ("/original", "/cache")
    trial.validate_bootstrap("/original", "/cache")
    trial.bind_server_id("/original", "ordinary-id")


def test_runtime_profile_selectors_are_private_only(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep normal ports and providers unchanged while restricting the trial profile."""
    defaults = {("airplay", False), ("chromecast", False), ("wiim", True)}

    monkeypatch.setattr(trial, "is_trial_target", lambda: False)
    assert trial.get_webserver_bind_port(8095) == 8095
    assert trial.get_default_providers(defaults) is defaults
    assert trial.should_setup_ingress() is True
    assert trial.should_load_builtin_provider("sendspin") is True
    assert trial.should_load_builtin_provider("builtin") is True

    monkeypatch.setattr(trial, "is_trial_target", lambda: True)
    assert trial.get_webserver_bind_port(8095) == trial.PRIVATE_API_PORT
    assert trial.get_default_providers(defaults) == {("airplay", False)}
    assert trial.should_setup_ingress() is False
    assert trial.should_load_builtin_provider("sendspin") is False
    assert trial.should_load_builtin_provider("sendspin_source") is False
    assert trial.should_load_builtin_provider("builtin") is True


def test_arbitrary_trial_path_rejected(app_data: Path) -> None:
    """No new generic path or seed interface is exposed."""
    with pytest.raises(trial.PrivateTrialError, match="fixed"):
        trial.prepare_data(str(app_data / "other"), str(app_data / ".cache"))


@pytest.fixture
def distribution(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Model own installed distribution metadata without installing anything."""
    package = tmp_path / "music_assistant"
    package.mkdir()
    origin = package / "__init__.py"
    origin.touch()
    metadata = tmp_path / "music_assistant-1.dist-info" / "direct_url.json"
    metadata.parent.mkdir()
    metadata.write_text(
        json.dumps(
            {
                "url": "https://github.com/nredd/server",
                "vcs_info": {"vcs": "git", "commit_id": "a" * 40},
            }
        )
    )
    dist = MagicMock()
    dist.files = [importlib.metadata.PackagePath("music_assistant-1.dist-info/direct_url.json")]
    dist.locate_file.side_effect = lambda path: tmp_path / path
    monkeypatch.setattr(importlib.metadata, "distribution", lambda _name: dist)
    monkeypatch.setattr(music_assistant, "__file__", str(origin))
    return metadata


def test_installer_provenance_fixed_safe_result(distribution: Path) -> None:
    """The validated installer SHA is not a claim about immutable loaded bytes."""
    assert distribution.is_file()
    assert trial.installer_provenance() == {
        "status": "verified-installer-provenance",
        "commit": "a" * 40,
    }


@pytest.mark.parametrize(
    "kind",
    [
        "url",
        "credential",
        "short",
        "nonhex",
        "editable",
        "shadow",
        "invalid",
        "large",
        "missing",
        "symlink",
        "array",
        "vcs",
        "duplicate",
        "parent-symlink",
    ],
)
def test_provenance_unverifiable_is_safe(
    distribution: Path, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    """Never expose URLs, metadata, credentials or paths on validation failures."""
    data = json.loads(distribution.read_text())
    if kind == "url":
        data["url"] = "https://github.com/other/server"
    elif kind == "credential":
        data["url"] = "https://secret@github.com/nredd/server"
    elif kind == "short":
        data["vcs_info"]["commit_id"] = "abcdef0"
    elif kind == "nonhex":
        data["vcs_info"]["commit_id"] = "z" * 40
    elif kind == "editable":
        data["dir_info"] = {"editable": True}
    elif kind == "vcs":
        data["vcs_info"] = "secret"
    distribution.write_text(json.dumps(data))
    if kind == "shadow":
        monkeypatch.setattr(music_assistant, "__file__", "/shadow/secret/__init__.py")
    elif kind == "invalid":
        distribution.write_text("secret-invalid-json")
    elif kind == "large":
        distribution.write_bytes(b"x" * (trial.MAX_JSON + 1))
    elif kind == "missing":
        distribution.unlink()
    elif kind == "symlink":
        distribution.unlink()
        distribution.symlink_to(distribution.parent / "secret")
    elif kind == "array":
        distribution.write_text("[]")
    elif kind == "duplicate":
        distribution.write_text('{"url":"secret","url":"https://github.com/nredd/server"}')
    elif kind == "parent-symlink":
        parent = distribution.parent
        target = parent.with_name("elsewhere")
        parent.rename(target)
        parent.symlink_to(target, target_is_directory=True)
    assert trial.installer_provenance() == {"status": "unverified"}
