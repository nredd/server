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
`869d9ebd37a09c2a8e1aef4d90bd8c34ba65b683`. Fresh verification found only the
config version changed to `2.11.0.dev2026102103`; the Dockerfile and entrypoint
are byte-identical to cached `56e9d912d977d5fdf45daa0266f19d311f76dd47`.
It source-installs at startup, shares the
production app's ports and supplies its own internal official secrets. Do not read,
extract or copy `app_secrets`, credentials or original databases. Do not redistribute
an image derived from the official server image. The old local DEV app in ERROR and
its previous seed are NOT a rollback target.

The private CLI proof branch is pinned to `5c4901f01834498babeb7af8e0f3b919aef15a20`.
Canonical CLI Build `39604837204` passed Linux native/cross, macOS, Python and sanitizer
jobs; Linux tests exercise the real inherited-FD/self-image entrypoint. Its AArch64 artifact
`12347767537` has ELF SHA256
`c65b4ddb067c85fafe461ab51eb9828e999eb0c7f3a7f87942a830cb35479199`.
The earlier `f41334b` run failed GCC's SHA256 buffer-extent check; that failure was fixed,
not waived. The new asset is staged alongside the old pin, never substituted into stock MA.
Its SSH-addon `--check` passed; its SSH-addon self-image probe failed closed. That musl
compatibility-loader environment is not the protected MA container, so it proves neither
protected success nor permission to skip the target preflight.

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

## Private runtime profile

On the Linux ARM trial target ONLY, the web API always binds to fixed port `18095`,
ignoring any stored webserver port. The controller adopts the port actually bound before
publishing its base URL, including an OS-selected fallback. It does NOT create the HA
Ingress listener or inspect interface addresses for Ingress discovery. The HA Ingress
panel is therefore unavailable for this trial. Use the direct private API/setup URL:
`http://<trial-host>:18095/setup` (or the controller's logged actual base URL). Normal
profiles retain their configured API port and HA Ingress listener.

The private provider profile auto-creates only `airplay`. It does not auto-create other
default providers, including mDNS-triggered device providers. It also neither creates nor
loads `sendspin` or `sendspin_source`, including retained private configs after restart.
Other builtins, including `builtin` URL radio and the trial's own internal `hass` provider,
retain their normal behavior. AirPlay therefore has no Sendspin bridge/web-player/device
claim path in this profile; existing absent-provider bridge paths remain graceful. This
is private runtime isolation, not a public configuration feature and not a generic safe
mode.

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
The build check executes a retained validated descriptor too, never a pathname which can
be swapped and restored between pre/post validation. It has a five-second deadline,
4096-byte combined-output limit and at most one
second of cleanup, and must report `cliairplay`, `check` and `v5c4901f` with exit zero.
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
metadata, paths, arguments, environment or credentials are returned. Every validated public
SHA1/SHA256 identity value (`commit`, `expected_sha256`, `validated_sha256` and matching
child `actual_sha256`) uses `{"encoding":"u32be-v1","words":[...]}` with exactly five or
eight unsigned big-endian 32-bit words. This survives normal diagnostics sanitization without
loosening it. This is INSTALLER provenance, NOT immutable loaded-byte or image proof. Compare
the commit with the EXTERNAL private receipt, never a tracked self-referential overlay SHA.

Startup validation is a historical claim, NOT current child attestation. Before any
private provider networking, the pinned CLI runs an `--attest-only` protected-self-read
preflight from the same retained validated asset FD. It must acknowledge the exact fresh
nonce and both pinned SHA-256 values, then intentionally exit zero. This preflight is
historical only, never a live-child receipt. Each private PTP, stream, and pairing launch
executes that same inherited read-only FD through `/proc/self/fd/<N>` with `pass_fds` and
must emit one bounded first stdout `[ATTEST]` JSON acknowledgement before receiver I/O.
The parent accepts it only when version, kind, nonce, both digests, and the exact owned
live process object match. A malformed, stale, wrong, late, or missing first
acknowledgement kills and reaps the owned child before stdout status, stderr `connected`,
readiness, or PCM production can proceed. The private process's stdin writer also rejects
all audio or pairing bytes until the exact live acknowledgement receipt exists, so a
concurrent producer cannot bypass the caller's start/readiness ordering. The direct ffmpeg
stdin-FD handoff also requires the same verified live receipt before exporting the pipe.
A repeated acknowledgement in subsequent stdout
is rejected by chunk, exact-size and line readers, revokes the receipt, and kills/reaps the
child. Private stdout is line-framed; over-limit records fail closed rather than being
silently discarded. Raw `communicate()` is unavailable for private children because it bypasses those
checked reader/writer paths. An owned exit watcher releases the retained executable FD on
natural termination too, even when a role's caller only polls its closed state.
This is point-in-time
`current-image-match-at-ack` evidence only, NOT a continuous image proof, parent-procfs
proof, AppArmor policy change, capability change, or Docker-protection bypass. Later diagnostics
require the same owned process object to remain live. Legacy non-FD-owned handles retain the
original bounded `/proc/<knownpid>/exe` check; an FD-launched private child without a complete
acknowledgement never falls back to that check. No arbitrary PID/path API,
process arguments, environment, IDs, raw output or secrets are exposed. Absent pairing
processes require no attestation; failures are `unverified` with only one finite reason:
`not-live`, `attestation-not-complete`, `procfs-unavailable-or-child-exited`, `procfs-access-denied`,
`executable-unstable-or-ineligible`, `procfs-read-failed` or
`child-changed-during-attestation`; `OSError` failures additionally retain only numeric
`errno`. A live readable different executable is `mismatch` with no unexpected digest.
Only a matching child includes encoded `actual_sha256`. The fixed asset is opened without
symlinks, validated, and retained as the executable descriptor; pathname replacement cannot
select a different initial exec target. The acknowledged self-image hash must still match the
external pin before readiness. This is not an immutable execution sandbox and does not detect
a later exec or memory change. None of these fields prove native UI controls or playback success.

## Gates before any launch

- Preserve a verified backup and restore route for protected official MA 2.10.7, including
  its original integration, credentials, data and speaker-group state. Supervisor partial
  backup `dc640e9f` contains stock MA `2.10.7`; older `8544fca8` contains `2.10.6` and is
  retained separately, not mislabeled as the current rollback pin. Neither includes HA Core.
  No seed from it
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
  controls and exact source/child identity remain unverified. The bounded `0af8b62`
  trial reached Linux private API readiness in 48.1 seconds with a distinct ID; a second
  no-playback trial completed normal fresh onboarding and verified old ports unbound.
  Its identity gate failed closed: full hashes were redacted by diagnostics and the
  PTP child was `unverified`. A later `c378319` trial verified the exact installed source
  and startup ELF, but its parent-procfs child read failed with `EACCES`; normal host audit
  logs confirmed same-profile AppArmor `ptrace` process-read denials in both directions. All trials restored stock
  through the independent recovery route, without playback. No policy or capability was changed. These observations do not establish native feature acceptance or an
  immutable installed image digest. Parent owns the full suite, review, external receipt,
  commit/push and live gates for each new source pin. Earlier observations do not authorize
  another source revision or playback.

## Private recovery controller

`scripts/private_airplay_recovery.py` is private HAOS recovery tooling. It uses the Python
stdlib and installed native `/usr/bin/curl`, only the SSH add-on's `SUPERVISOR_TOKEN`,
and fixed add-on slugs and target entity
IDs. It never accepts an API host, endpoint, credential, media URL, queue, or raw add-on
configuration as an argument or stores any of those values. Never add it, its tests, or its
runbook procedure to upstream work.

Copy the reviewed helper to the SSH add-on, verify its SHA-256 against the external
receipt, and set `RECOVERY_SCRIPT` to that verified absolute path. It supports the SSH
app's Python 3.13 independently of the server's Python 3.14. Read-only capture has been
exercised against the actual Supervisor/Core wire responses; that is not restoration proof.

Before a short authorized trial window, run this **on HAOS from the SSH add-on** while the
fresh target state is known, not from a historical snapshot:

```sh
python3 "$RECOVERY_SCRIPT" capture --target main
# prints /data/airplay-native-probe/recovery/<fresh-uuid>
python3 "$RECOVERY_SCRIPT" arm --run <fresh-uuid> --timeout 120
```

`capture --target main|office|both` permits only those fixed target scopes and defaults to
`both`. A single-target run never snapshots or restores the other target; it still refuses an
active non-target MA stream. `capture` rejects an unprotected official add-on or any installed version other than exact
`2.10.7`, target groups containing non-target players, malformed state, unsupported play state,
and any other active Music Assistant media player. It writes a new UUID directory only beneath
the dedicated `recovery` subdirectory, with `0700` directory and `0600` state permissions. It
captures only target group membership, volume, play state, and the official/DEV app
started/protected/version facts. It does not recover queues or media URLs. In particular,
an idle ungrouped snapshot means restore must keep the players idle and ungrouped, not resume
an older queue. This controller is HA/add-on scoped only: HA's idle state does not verify a
native receiver, its direct reachability, its independent receiver volume, or peer grouping.
Do not overwrite receiver volume from HA `volume_level`, fan peer telemetry into groups, or
claim peer restoration from this receipt.

`arm` uses native `posix_spawn` to exec a detached, new-session watchdog with only
`SUPERVISOR_TOKEN` in its environment. No Python runs between fork and exec, including
when the caller has other threads.
It validates its UUID and nonce and invokes the same restore path at the hard deadline. Only
arming requires a fresh snapshot; an already armed watchdog never refuses an emergency restore
solely because its snapshot aged. On
manual failure or deadline, restore stops DEV first, starts and checks official MA, and
reloads its existing HA config entry only when this controller started the app. This resets
HA's reconnect backoff without changing the entry's configuration or credentials or starting
any discovery flow. It waits for the selected targets, restores groups/levels/play state,
then verifies them before writing its
owner-only receipt. A missing or `verified: false` receipt is a failed recovery -- do not
claim success. A process-held application-wide lease prevents overlapping watchdogs for
different run IDs. A matching verified completion receipt prevents a delayed old watchdog
from stopping a subsequent trial. The lease remains held until that watchdog exits.
The watchdog is independent of the DEV app, but mock tests are not real HAOS restoration
evidence. A real short restart drill succeeded. A longer probe exposed shared HA
reconnect backoff: both the target and another MA entity returned 159.4 seconds after MA
restart, exceeding the controller's 90-second deadline. That failed receipt remains failed;
it is not counted as successful restoration. The scoped existing-entry reload addresses that
reconnect boundary, rather than suppressing the timeout or weakening state verification.
Verify the revised path in a real same-duration drill before launching DEV.

Run the nondestructive detached-child path before the live window:

```sh
python3 "$RECOVERY_SCRIPT" self-test
```

This creates a temporary private run and uses only its in-process fake transport in the
child. It proves child detachment, run ownership, timeout wakeup, restore ordering and
receipt handling without calling HAOS or a speaker. It is not a live restore test.

### Recovery hardening notes

Manual `restore` and a deadline watchdog take the same bounded, application-wide mutation
lock before checking a completion receipt or changing DEV/official MA. This prevents one
run's manual restore from racing another run's watchdog. A failed receipt remains private
failure evidence, but the same run ID and nonce may atomically replace its own `verified:
false` receipt with `verified: true` only after a later restore verifies; foreign or malformed
receipts never become successful.

Before reloading the existing HA entry after starting official MA, recovery waits within the
same 90-second deadline for the fixed unauthenticated local MA `/info` endpoint. It retries
only documented temporary availability responses. Target-state reads similarly retry only
Core `404`, `502`, and `503`; authentication, schema, and other failures remain fail-closed.

A snapshot's HA group member order is preserved because it may encode the leader. Recovery
skips player service commands when the observed HA state already matches. It never maps HA
`volume_level` to an independent native receiver level. A changed stored HA volume fails
closed instead of issuing `volume_set`; restore receipts cover HA app/state only, not native
peers, receiver reachability, native volume, or peer grouping. The parent must separately
verify any explicitly authorized native-peer restoration.

The fake transport is available to a detached `self-test-child` only when that exact armed
run has an owner-only simulation marker bound to its UUID and nonce. An ordinary production
snapshot cannot select fake transport. Snapshot schema 1 remains valid for emergency
restores.

The controller requires the installed native `/usr/bin/curl` (HAOS 8.22.0) and is therefore
not pure-Python HTTP tooling. Each fixed Supervisor or local readiness request uses curl with
`--disable`, a cleared child environment, a validated fixed path, `--max-time` and a matching
parent timeout from the same remaining absolute deadline, a `65536`-byte response cap, and no
redirect or TLS-relaxation options. The authorization header is supplied only through curl's
stdin configuration, never arguments, files, logs, or inherited environment. Slow headers,
slow bodies, DNS, connection, and transfer work cannot extend the 90-second recovery budget.

`arm` rejects any existing `receipt.json`, including a broken receipt symlink. A manually
completed run must receive a new capture UUID before arming; this prevents an old no-op
watchdog receipt from leaving a later DEV trial without protection. A watchdog that was
already armed still treats its own matching verified receipt as a settled no-op after it
acquires the mutation lock.

Failed receipts are bounded, exact-schema, nonce-bound evidence. Before a same-run verified
promotion, recovery exclusively archives the false receipt as `failed_receipt.json` (`0600`),
then atomically replaces the live receipt. The verified receipt explicitly scopes verification
to HA app/state and excludes native peers. Mutation-lock waiting consumes the same absolute
90-second recovery budget and rechecks the settled receipt only after lock acquisition.

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
