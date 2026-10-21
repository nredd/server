# Private official DEV trial overlay

NOT ready to deploy. NEVER include this overlay, its tests or this runbook in the upstream
native-controls feature PR. This is deliberate private source selection, not a supported
binary-path setting, downloader, image distribution or production deployment procedure.

## Pins and scope

`private-native-airplay-trial-pin.json` records the accepted CLI source SHA, ELF digest,
build label, source base and official app contract revision. This checked-in template
keeps `server_overlay_sha` null and `launch_authorized` false. After committing the
reviewed overlay, record its exact server FULLSHA, this manifest's digest, explicit owner
approval and the agreed deadline in a separate private trial receipt. Do not put the
commit's own hash in its tracked contents. No launch is authorized by this template.
Use `server_repo: nredd/server@FULLSHA`,
never a floating branch. Keep `frontend_repo: ""` to use the bundled frontend.

The official DEV app contract cached at
`/Users/redd/code/airplay-cli/build/upstream-app-contract` is revision
`56e9d912d977d5fdf45daa0266f19d311f76dd47`. It source-installs at startup, shares the
production app's ports and supplies its own internal official secrets. Do not read,
extract or copy `app_secrets`, credentials or original databases. Do not redistribute
an image derived from the official server image. The old local DEV app in ERROR and
its previous seed are NOT a rollback target.

## Private bootstrap and HA isolation

On this overlay's Linux ARM profile ONLY, the official entrypoint contract
`--data-dir /data --cache-dir /data/.cache` is redirected before options, logs or config
reads to `/data/.native-radio-trial-7d964670ba5d42fd90a3a709c71ec462/data` and its `.cache`.
This fixed one-trial namespace belongs to the DEV app's OWN `/data`; there is no path,
environment or API override and no copy from root data. Other platforms are unchanged.
The overlay does not read root `options.json`, settings, databases or official secrets.
Official secrets remain internally managed by the official app; never extract or copy them.

First boot requires successful exclusive creation of the namespace and an empty directory.
An existing empty directory, seed, symlink, unknown top-level content, incomplete marker,
wrong marker version or mismatched server ID fails closed; nothing unknown is erased.
MA's existing config controller generates its default fresh UUID. Before discovery is
created, its own settings are flushed and the ID is bound to the private versioned marker.
A restart requires that marker and the same server ID in the namespace's own settings.
Bounded JSON must pass MA's actual parser as well as duplicate-key validation, so invalid
primary settings cannot trigger unchecked backup loading before the identity guard.
An interrupted initial bootstrap deliberately requires owner investigation, not automatic
repair or adoption. This is an offline bootstrap invariant, NOT evidence that HAOS has
actually booted this namespace. A distinct live server ID must still be verified privately
against the original before playback. The ID is not added to diagnostics or startup logs.

Both trial publication paths are guarded before work: `_mass._tcp` registration/update
and Supervisor `/discovery`, including before HA system-token creation. Cold setup,
core-state refresh and the periodic HA announcer cannot publish this trial; its periodic
HA timer is not scheduled. Provider service browsing/callbacks, AirPlay DACP registration
and PTP are unchanged. Never accept any trial HA integration. This is application-level
publication suppression, not a network sandbox or proof of an actual HA flow outcome.

## Validation and diagnostics

On Linux AArch64 (including the `arm64` alias), every `get_cli_binary()` call validates
ONLY the content-addressed `/media/.native-airplay/<expectedSHA>/cliairplay-linux-aarch64`.
The overlay is a read-only CONSUMER of media, even though the official app mounts it RW.
It never writes media, changes the packaged stock executable, downloads a binary or
falls back to stock. All path components must be non-symlinks; the bounded regular
executable must match SHA256 and ELF64 little-endian AArch64 headers before `--check`.
The check has a five-second deadline, 4096-byte combined-output limit and at most one
second of cleanup, and must report `cliairplay`, `check` and `v5b3dcc4` with exit zero.
Only the fixed build label is retained, never arbitrary check output. The bytes are
validated again after the check. There is no successful-validation cache on mutable media.
Delayed pairing revalidates immediately before constructing its execution arguments.
Provider startup fails before registration for an invalid pin; the PTP catch cannot
turn a pin failure into degraded NTP. PTP readiness policy, transport, metadata and
group volume are unchanged. macOS and other nontrial platforms explicitly retain
packaged-binary behavior.

AirPlay diagnostics add `trial_binary`: mode, expected digest, safe startup asset
validation (validated digest, fixed check label and outcome), and owned child roles.
Installer provenance is a separate `installer_provenance` field. It reads only this
process's `music-assistant` distribution's bounded PEP 610 `direct_url.json`, requires
Git from the fixed public `https://github.com/nredd/server` repository and a full lowercase
40-hex commit, and compares the loaded package origin with the distribution package root.
Editable, shadowed, missing, malformed or unverifiable metadata returns only `unverified`.
Success returns only `verified-installer-provenance` and the validated commit; no raw URL,
metadata, paths, arguments, environment or credentials are returned. This is INSTALLER
provenance, NOT immutable loaded-byte or image proof. Compare the commit with the
EXTERNAL private receipt, never a tracked self-referential overlay SHA.

Startup validation is a historical claim, NOT current child attestation. Each available
owned stream/PTP/pairing process is separately checked through `/proc/<knownpid>/exe`
using bounded reads and pre/post process-liveness checks. No arbitrary PID/path API,
process arguments, environment, IDs, raw output or secrets are exposed. Absent pairing
processes require no attestation; exit, races and unavailable procfs are `unverified`,
never `match`. A live readable different executable is `mismatch`. Selection uses a
mutable pathname, not an immutable execution sandbox: an adversarial concurrent media
replacement after validation remains possible, so live child attestation is a separate
required gate. None of these fields prove native UI controls or playback success.

## Gates before any launch

- Preserve a verified backup and restore route for protected official MA 2.10.7, including
  its original integration, credentials, data and speaker-group state. No seed from it
  goes into DEV. Use fresh private DEV data and only fresh public SomaFM radio.
- Resolve the startup budget before launch: pinned source install happens at startup,
  so a two-minute listening window is NOT a build-time estimate. Establish a hard
  startup deadline and an independent watchdog that restores the official app even if
  DEV never becomes ready. No startup-time measurement or HA trial has happened here.
- HA core 2026.9.2's matching cached `config_flow.py` confirms a NEW server ID always
  enters `hassio_confirm`/`discovery_confirm`; a SAME ID can update the original token/URL.
  Fresh MA config controller 83 defaults to `uuid4`. The fixed private namespace and
  trial publication guards above must pass before any discovery starts. Verify the actual
  distinct live ID privately before playback, never accept the trial HA integration,
  and abort ONLY its trial discovery/confirmation flow if one nevertheless appears.
  Do not alter the original HA integration. Installed=false by itself does NOT prove
  absent stale/orphaned/private data or isolation, and the old local ERROR app/seed must
  never be reused. No new auth component or invented MA config key is involved. The cached flow source is
  `ha-core-2026.9.2-music-assistant-config-flow.py` in the contract directory.
- Preflight target ownership and non-target MA streams. Explicitly approve an exclusive
  start window: stock MA and DEV cannot share service/PTP ports or own the target group
  concurrently. Stop DEV FIRST on success, deadline or failure, then restore official
  MA and the prior group, and verify restoration. Keep the watchdog independent of DEV.
- Require the parent's full suite, reviewed server FULLSHA/digest/pin manifest, actual
  Linux startup/child-attestation evidence, full installed-source/image identity,
  discovery isolation and owner approval. Supplied read-only official DEV info was
  installed=false/available=true/build=true/latest=2.11.0.dev2026102003, with HAOS 18.2 and
  HA core 2026.9.2; this is NOT a freshness or launch receipt. Native UI, real station/group
  controls, source/image identity, actual Linux startup and startup duration remain
  unverified. Parent owns final full suite, review, external receipt, commit/push and live
  gates. No deployment, live setting change or installer execution is part of this work.

## Local checks

Use the existing main venv without syncing or pruning provider dependencies, but import
ONLY this worktree's source from this worktree's current directory:

```sh
/Users/redd/code/ma-server/.venv/bin/python -c 'import music_assistant; from pathlib import Path; print(music_assistant.__file__); assert Path(music_assistant.__file__).resolve().is_relative_to(Path.cwd())'
/Users/redd/code/ma-server/.venv/bin/python -m pytest tests/providers/airplay
```

Expanded `ty` scope includes the newly touched `__main__.py` and `mass.py`; those two
files already report 13 diagnostics (12 errors and one deprecation warning) on pristine
`dfe07c8`, reproduced from a complete `git archive` with the same interpreter/config.
The overlay adds no diagnostics. The other changed Python files pass scoped `ty`, and
full pre-commit including canonical mypy passes. This is NOT an all-green expanded
`ty` gate; the existing baseline diagnostics remain explicitly recorded.

The late-join fixture blocker was repaired separately: its constructor now receives the
real PCM format before deriving byte/frame rates. Frozen-timeline regressions cover
anchors ahead of, at and inside the write head, without changing production clocks.
The final local suite passed 22,233 tests with 23 existing skips and 76 snapshots;
canonical pre-commit/mypy and private-module `ty` also passed.

The fork's workflow registry initially exposed no workflows despite Actions being enabled
and `test.yml` existing on `dev`, so manual dispatch returned 404. The private-only
`private-native-trial.yml` push wrapper calls that existing canonical reusable workflow
for the exact pushed trial SHA and isolated fixture commit
`cd90698e52e83238446d021f558359c7ac37ac35`. It changes no repository settings or gates.
CI success must still be verified; a local pass is not a CI or Linux startup claim.

Synthetic ELF tests validate bytes but NEVER execute Linux ELF on macOS. Bounded check
and liveness tests use real local children; synthetic procfs covers digest comparisons
on macOS, and real procfs is used when available. No skipped tests or weakened signing
hooks are needed. Local tests are not a deployment or native UI success claim.
