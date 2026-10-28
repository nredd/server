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

The private CLI observation branch is pinned to `0531c6cc17627125ff73f18640a1f8621cf75b3c`
(merge of nredd/airplay-cli#4, strict-peer attestation on the daemon ready line, on top of
`d286032` strict-peer receive gate and `716f894` passive capture). Canonical CLI Build
`40749913557` passed the Linux aarch64/x86_64 platform jobs and the sanitizer job; the
macOS jobs stalled in GitHub's hosted-runner queue and the identical `build-macos (arm64)`
recipe (`make STATIC=1`, `make test`, `--check`) was run locally on an arm64 Mac instead,
all green, before the merge. The aarch64 artifact has ELF SHA256
`0d069870df129136eb5c52ee41e665124c62e2b37231acee02676b1c2144de47` (6173544 bytes) and
build label `v0531c6c`. The previous `716f894` asset (`c5e397d3…f792`) stays staged on the
host as history; it cannot attest strict peers and is no longer the pin. The new asset is
verified locally only until the SSH-addon `--check` and self-image probe are rerun on it.

## Private aggregate volume capture

The advanced `private_native_volume_capture` provider flag exists only in this private
profile and defaults to false. It is an additive private configuration value, not a
normal-provider option or an environment inherited by all children. The operator must set
its value explicitly for each window and restore it afterward. Only a private, eligible
AirPlay 2 stream with the existing HomePod-remote option explicitly enabled can request
`CLIAIRPLAY_PRIVATE_NATIVE_VOLUME_CAPTURE=1`; other child launches explicitly receive `0`.
Capture does not enable logical volume, attach transports, subscribe to updates or publish
availability.

The existing provider diagnostics retain a bounded history of scalar CLI summaries and
finite DACP classes, never UID values, request headers/bodies, Active-Remote values or raw
status/error lines. DACP classification precedes suppression and numeric conversion. Raw
DACP logging is suppressed provider-wide during capture, including unmapped and malformed
requests; value-bearing parse failures invalidate the capture without escaping to server
exception logs. Legitimate DACP actions remain unchanged.

The private `airplay/private_native_volume_capture/get` command requires `SYSTEM_MANAGE`
and returns only `{"records": [...]}` from the bounded history, including after the player
stream is cleared. The separately fixed
`airplay/private_native_volume_capture/readiness/get` command has the same scope and returns
installer provenance, encoded expected/startup asset receipts, live owned `ptp`/`stream`
attestation receipts and the finite readiness facts below. It excludes diagnostics, logs, paths,
arguments, identities, pairing state, publication and command data. Both commands are registered
only on the private target, reject direct calls outside that target and unregister on provider
unload. Registration is transactional during async initialization, not the core's exception-swallowing
post-load hook: a second-name collision releases the first claim, retains the incumbent, cleans up
provider resources and fails loading. The coordinator must use these narrow commands, never broad
`diagnostics/get`, which includes unrelated sections and exceptions when log-tail collection is disabled.

Readiness schema version `1` has exactly these top-level fields:

- `schema_version`: `1`.
- `installer_provenance`, `expected_sha256`, `startup_validation`, `owned_children`: the existing
  finite provenance/asset/child receipt schemas, with only `ptp` and `stream` roles.
- `snapshot_current`: boolean. Provenance is collected first, then exact PTP/event and
  player/stream/child/request/capture references are snapshotted. After ALL awaits, the same
  references, stream roster and each `TrialAsyncProcess.has_current_receipt()` are checked
  synchronously. Replacement, exit or revocation invalidates the snapshot and downgrades every
  child receipt to `unverified` / `child-changed-during-attestation`.
- `ptp_ready`: boolean, requiring a current snapshot, live acknowledged PTP child and its
  readiness event set. An alive process alone does not qualify.
- `prestream_ready`: boolean, requiring `ptp_ready` and no streams. It does NOT promise the
  future stream's flags or actual route.
- `playing_ready`: boolean, requiring `ptp_ready`, at least one stream and every stream `ready`.
- `streams`: list of the following finite rows, with no player IDs or role bindings:
  - `generation`: positive capture-generation integer or null; the same process-local generation
    appears in historical capture records. It is not a stable device identity or cross-restart key.
  - `current`, `connected`, `playing`, `ready`: booleans. `playing` additionally requires a playing
    report from this launch, not a previous child's retained player state.
  - `capture_enabled`, `homepod_remote_requested`, `session_volume_requested`: booleans or null
    if there is no exact-child request snapshot. These are retained from the actual launch
    environment, not later provider configuration.
  - `homepod_remote_admission`: boolean or null if unreported, from this child's PRE-SETUP
    status. This is policy admission only, not native UI success.
  - `protocol`: `airplay2`, `raop` or `unknown`; `flow`: `realtime`, `buffered` or `unknown`;
    `timing`: `ptp`, `ntp` or `unknown`. These are allowlisted CLI route reports, reset per
    launch, never raw route strings or inferred configured modes. The CLI reports `flow=native`;
    the API normalizes that with `buffered=0` to `realtime` and with `buffered=1` to `buffered`.
    Legacy/compatibility flows, malformed markers and unknown values fail closed. The marker
    reports the resolved PRE-CONNECT choice, not proof of receiver clock lock or absence of a
    later fallback; `ptp_ready` separately describes the acknowledged daemon only.

A ready row requires current ownership/acknowledgement, an active exact-child capture generation,
`capture_enabled=true`, requested and reported HomePod admission true,
`session_volume_requested=false`, actual `airplay2/realtime/ptp`, connected and playing.
Unknown or unsupported facts fail closed. The operator must additionally compare provenance and
asset receipts with its external receipt, enforce the expected stream count and retain the exact
capture generations for final-record matching. All readiness claims are point-in-time only: they
provide neither continuous authority nor interval coverage, native UI acceptance or verified output
identity. No subscription, logical-volume publication, role binding or production availability is
activated by this API.

A valid final record is historical observation from the exact acknowledged child, not
current identity, readiness or command authority. Natural exit preserves that history;
revoked acknowledgements, stale actors, force-kill, missing/duplicate/malformed summaries,
overflow and unavailable planes cannot become successful evidence. A terminal audio status
does not end collection before the later shutdown summary. Production records remain
provisional until stdout reaches validated EOF too. Graceful stop gives that validator a
bounded two-second tail before cancellation; timeout/cancellation marks `incomplete-stdout`,
and late acknowledgement revocation invalidates even an already detached record. Detached
records survive stream cleanup without retaining the child reference.
`planes_connected_at_cutoff` describes only
the final snapshot; `interval_coverage_verified` remains false because this aggregate does
not prove uninterrupted coverage or gesture-to-packet causality. Native UI/group volume
acceptance still requires the independent bounded hardware loop and real binding proof.

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
HA timer is not scheduled. AirPlay DACP registration and PTP daemon startup are unchanged
advertisement/listening. Never accept any trial HA integration. This is application-level
publication suppression, not a network sandbox or proof of an actual HA flow outcome.

## Main-only discovery admission

The trial profile may resolve, probe and register exactly ONE device, named by the private
advanced discovery core entry `private_trial_target` as `<12 lowercase hex MAC>@<advertised
name>` (e.g. `aabbccddeeff@Main Pod`). The entry exists only on the trial target. While it is
unset, blank or malformed the profile fails closed: no directed mDNS query, no related-service
lookup, no `/info` probe and no player registration happens for ANY device, including the
intended one. It is read live, so set it through the private API and then reload the
`airplay` provider to replay the shared mDNS cache under the new admission. Changing the
target while the provider runs is unsupported: the old device's records are no longer
admitted (not even its `Removed` event), so its registered player would linger until the
provider reload unregisters it. Retarget means reload.

Enforcement seams, each with offline RED/GREEN tests and no ordinary-profile change:

- `DiscoveryController.admits_mdns_record` decides before every `AsyncServiceInfo.async_request`
  in browser state changes, provider replay and `async_find_mdns_service`, including the
  unfiltered related-service lookups. Off-target records are logged at VERBOSE and dropped;
  even query-free `Removed` events never reach a provider. The instance label is taken in
  front of the service type being resolved, never in front of the first dot, so
  `Main.Office` is not `Main`. A `_raop._tcp` label is admitted only with the target's
  `<mac>@` prefix; a MAC-less RAOP label is never the target.
- This change narrows the global browser from every manifest's service types to those of
  providers this profile can load. Type browsing is ordinary multicast PTR traffic, not a
  directed query.
- `AirPlayProvider._setup_player` rejects any resolved `player_id` other than
  `ap<MAC>` before the disabled check, the receiver filter, related-service lookup or
  registration, so a same-named foreign record cannot pass on name alone.
- `AirPlayProvider._learn_audio_formats` (direct `/info`) and the address-based cached
  control lookup consult the same admission.
- Every owned private CLI child (PTP daemon, stream and pair-setup) receives
  `CLIAIRPLAY_PTP_STRICT_PEERS=1`, and on the trial target the shared daemon counts as
  ready ONLY when its `[PTP] daemon up` line attests `strict-peers=on`. The pinned asset
  `0531c6c` implements the receive gate (`d286032`) and the attestation (#4). An older
  asset that cannot attest it (e.g. the retired `716f894`) leaves `ptp_ready`,
  `prestream_ready` and `playing_ready` false, fails the readiness receipt closed, and
  makes every trial AirPlay 2 session start wait the full `PTP_DAEMON_READY_TIMEOUT` (3 s)
  and run without the shared clock; that is the intended consequence of refusing an
  unattested gate, not a regression to work around.

`scripts/private_trial_window.sh` is the operator wizard for one live window, shaped by
the first live runs on 2026-10-28. It verifies the CLI pin (hardcoded) against the asset
on the host and its `--check` label, and requires the DEV add-on's `server_repo` option to
equal the running checkout's `HEAD` with `boot: manual`, watchdog off and protection on:
the add-on source-installs that option at startup, and a stale pin once ran a build
without the admission seams against the office pod. It requires the by-hand sibling stop
via `preflight`; resolves the office pod's Ethernet MAC from the kernel neighbour table
(the mDNS device id `fa:38:…` is NOT the link MAC `40:ed:…`, which a control probe
exposed); records a DEV-stopped baseline because HA Core itself talks HomeKit to the pod
over IPv6 continuously; starts `tcpdump` on `end0` keyed on that MAC plus the office IPs,
unicast only; proves the filter with a one-ping control probe before the window epoch;
runs lifecycle capture/arm/start; waits for the private API; checks that no AirPlay
player registered without a target; onboards or logs in; saves `private_trial_target`
and reloads AirPlay through the API; requires `players/all` to list exactly
`ap323e659fe4e1`; prints the readiness receipt; optionally plays a stream to main-pod;
stops DEV; and prints the verdict as baseline-subtracted flows after the epoch with
explicit `HOST_TO_OFFICE_IPV4` and `AIRPLAY_PORTS` counts. A failed fail-closed, isolation
or readiness check skips playback and goes straight to stop. An `EXIT` trap stops DEV
(receipted, idempotent) and the capture on any abort. Acceptance: both counts zero and no
non-baseline flow from the host except the control-probe ARP in the first second.

Live results 2026-10-28 (evidence `/data/airplay-native-probe/WINDOW_EVIDENCE_20261028.json`
on the host, pcaps 0600): the old `13df592bf` source registered office-pod and hit it on
tcp/7000 and 49153 (295 frames); `9b6639899` with no target registered no AirPlay player
and sent nothing; `9b6639899` with the target registered exactly main-pod, attested
`v0531c6c` with `ptp_ready` true, played AirPlay 2 realtime/PTP audio to main-pod for
about 75 s, and sent zero frames to the office devices beyond the control ARP. The
capture flag and HomePod-remote admission were off, so `playing_ready` stayed false.

Admission is a per-record/per-id filter on this application's own traffic. It is not a
network sandbox, does not stop receivers from multicasting to this host and does not prove
by itself that no packet left for another device; that proof needs a capture on the live
host during an approved window. Residual: a foreign `_airplay`/`_companion-link`/
`_mediaremotetv` record with EXACTLY the target's instance label would receive one directed
query before its resolved `deviceid` is rejected in `_setup_player`; mDNS name-conflict
resolution keeps two live instances from sharing one label on a link, and the office pod
does not share the main pod's name.


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
second of cleanup, and must report `cliairplay`, `check` and `v0531c6c` with exit zero.
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
stdin-FD handoff also requires the same verified live receipt before exporting the pipe. Private
command FIFO writes recheck that the same private process instance still has a live receipt after
opening the FIFO and immediately before each OS write, so a command queued behind another write is
dropped if an exit or duplicate acknowledgement revokes the receipt first. This cannot make process
liveness and an OS write atomic, or turn the point-in-time receipt into continuous execution or
memory-integrity proof.
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

The private overlay includes the upstream-shaped, default-off logical session-volume backend
only as a composite local integration. It starts disabled, requires both experimental options,
a current private child acknowledgement, the v1 CLI handshake, exact current source ownership,
confirmed backend receipts, and the offline-only `verified_identity=True` binding seam. No
private call site binds an output identity. The attestation-gated command FIFO does not publish
native availability or infer any UID equivalence. This source is NOT deployed or authorized for
a launch, and group/session native volume and native UI controls remain unaccepted. The separate
main-pod radio trial has owner-confirmed station-first/song/art/LIVE metadata and physical
Apple Music button volume audibility, but it does not establish native-state synchronization,
Control Other Speakers volume, grouping, slider routing, or this backend's identity binding.

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
  DEV never becomes ready. The earlier private boot reached its API in 48.1 seconds;
  that measurement is not a readiness guarantee for a different source or cold start.
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
  another source revision or playback. Protected source `c1969e478f77ff51486e6b73808057db1d1d18c4`
  subsequently passed installed-source, startup ELF and live owned PTP
  `current-image-match-at-ack` verification without policy changes, and restored stock.
  A later bounded real-radio window also reached an attested PTP stream with native
  station-first/song metadata. Its initial grouped restoration expired; a later watchdog
  restored the captured HA group. That failure remains recorded, and neither runtime
  metadata nor later recovery proves native UI/volume acceptance.

## Private stock-absent DEV lifecycle controller

The owner removed stock MA `2.10.7` from HAOS on purpose (Supervisor backup `23381989`,
off-device copy and restore notes kept privately). Stock must NOT be restored automatically.
The historical recovery controller below assumed stock installed, started stock and restored
HA groups; it is INVALID for this state and must not be armed. `scripts/private_dev_lifecycle.py`
(schema `2`, root `/data/airplay-native-probe/dev-lifecycle`) is the replacement. It is
standalone on the SSH app's Python 3.13 with stdlib plus native `/usr/bin/curl`, and it
never imports or calls the historical helper, whose bundles, receipts and failures stay as
they are.

What it can and cannot do is fixed by its method/path allowlist: `GET /addons`,
`GET /addons/<DEV>/info`, `POST /addons/<DEV>/start`, `POST /addons/<DEV>/stop`. There is
no stock path, no `/store` path and no Core API path, so it cannot install or start stock,
reload an HA entry, or group, play, stop or set volume on ANY player. Office and
historical-group exclusion are therefore structural, not runtime checks.

Copy the reviewed script to the SSH add-on, verify its SHA-256 against the external
receipt for the reviewed commit, and set `LIFECYCLE_SCRIPT` to that verified absolute
path, exactly as for the historical helper. The receipt binding lives outside this repo.

Stock absence is a fact about the COMPLETE installed-app catalog (`GET /addons`), validated
as a non-empty list of entries with string `slug`/`state`/`version` and unique slugs. The
stock slug must be absent and the DEV slug present. Other `music_assistant` slugs (the
local `2.10.6-cliairplay0.5.7` dev build, the `2.11.0b2` beta) may stay installed but
must read `stopped` in the catalog: a running sibling owns the shared host ports
(`8095`, DACP, PTP 319/320) and the speakers, and this controller can never stop it. The
operator stops it by hand before the window, ideally with `boot: manual` so an HA restart
cannot resurrect it mid-window. The controller checks only the catalog state, not the
sibling's `boot` mode; a sibling that comes back mid-window is detected at stop time and
turns the run into a failed receipt (DEV is still stopped first). The per-app `info` fallback for an uninstalled app is never consulted
or modelled for absence. DEV's own `info` must then report a confirmed `started`/`stopped`
state, `protected: true`, `boot: manual` and `watchdog: false`; `startup`, `error`,
`unknown` or missing values fail closed.

```sh
python3 "$LIFECYCLE_SCRIPT" preflight            # read-only facts, no run created
python3 "$LIFECYCLE_SCRIPT" capture              # requires DEV stopped; prints run dir
python3 "$LIFECYCLE_SCRIPT" arm --run <uuid> --timeout 120
python3 "$LIFECYCLE_SCRIPT" start --run <uuid>   # only inside a live armed run
python3 "$LIFECYCLE_SCRIPT" stop --run <uuid>    # manual early stop, same path as the watchdog
python3 "$LIFECYCLE_SCRIPT" self-test            # fake transport only, no HAOS calls
```

`capture` stores only the sorted installed slugs and DEV version under a fresh UUID with
`0700`/`0600` permissions. `arm` detaches the same `posix_spawn` watchdog as before (only
`SUPERVISOR_TOKEN` in its environment, one application-wide lease, timeout bounded to one
hour). `arm` holds the mutation lock from the lease write through the spawn, and the
held lease is written with the run's id and nonce. `start` runs under the same mutation
lock and requires that exact run to be armed and unexpired, receipt-free and not a
self-test run, AND proves a child still holds the lease for this run (a `watchdog.json`
left by a failed spawn or a dead child is refused). It then re-reads the facts, refuses
any catalog or DEV version drift, requires DEV `stopped`, and only then requests the
start. `startup` is pending; only an observed `started` within the 90-second budget
confirms it, and `error` or anything else fails.

The watchdog/`stop` path stops DEV FIRST, before any catalog check, so a drifted catalog
(even stock reappearing) can never leave DEV running. Every observation that is not
exactly `stopped` (`started`, `startup`, `error`, `unknown`, or a failed read) gets a
stop request, re-sent every five seconds until the budget ends; HTTP 5xx/429 and curl
transport failures are transient and retried, a refused stop is simply re-sent. A stop
response is not stop evidence: an observation must read exactly `stopped`. Waiting for
the mutation lock has its own ten-minute budget and the stop gets a full 90-second budget
only after the lock is held, so a slow `start` cannot starve it. If the lock cannot be
opened, validated or acquired in time, the stop is still attempted UNLOCKED (it is
idempotent and safety wins over serialization) and the lock failure is the recorded
failed receipt. A sleeping watchdog wakes every five seconds and exits early once a
manual `stop` has verified its run, releasing the lease. Only after the stop is stock
absence re-verified against the captured facts. The receipt is `verified: true` with the fixed status
`DEV stopped and stock absence re-verified; nothing else touched` only when both hold;
any other outcome writes a failed receipt. The first failure stays the evidence of
record (a repeat failure neither replaces it nor raises a conflict); it is archived as
`failed_receipt.json` only when the same run later verifies. Historical schema-1 receipts
and statuses never settle a schema-2 run. Arming an already-receipted run is refused;
capture a fresh UUID.

None of this authorizes a launch. `launch_authorized` in the pin stays false, main-only
isolation is unproven on hardware, and the owner must approve a fresh window.

## Private recovery controller

RETIRED for the stock-absent state: this controller belongs to the EARLIER stock-installed
state. Do not arm it while stock is absent: its capture preflight fails closed on the
missing stock app, and its restore path would start stock and restore HA groups, both of
which are now forbidden. The text below is kept as it was.

`scripts/private_airplay_recovery.py` is private HAOS recovery tooling. It uses the Python
stdlib and installed native `/usr/bin/curl`, only the SSH add-on's `SUPERVISOR_TOKEN`,
and fixed add-on slugs and target entity
IDs. It never accepts an API host, endpoint, credential, media URL, queue, or raw add-on
configuration as an argument or stores any of those values. Never add it, its tests, or its
runbook procedure to upstream work.

Future hardware acceptance is `media_player.mainpod` ONLY. `media_player.officepod`
(downstairs) is excluded from playback, volume, transport, grouping, power, native probes,
and any request to interrupt TV/audio. Future office/group acceptance requirements are
retired, not completed. Historical artifacts, pins, and offline group fixtures remain
unchanged; they do not authorize office hardware use. Keep the existing active non-target
stream guard: never clear office activity to enable a main trial. Safe main-only isolation
redesign is separate task 70, not part of this offline recovery fix.

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
`both`. Capture requires confirmed official `started` and installed DEV exactly `stopped`.
The shared add-on state predicate rejects `error`, `starting`, `stopping`, `unknown`, empty
or malformed states, including an uninstalled catalog response; not `started` alone is NOT
stopped evidence. A single-target run never snapshots or restores the other target; it
still refuses an active non-target MA stream. `capture` rejects an unprotected official add-on or any installed version other than exact
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
manual failure or deadline, restore stops DEV first, starts and checks official MA. DEV receives
a normal stop request only from confirmed `started`; both an already-stopped DEV and the
post-stop observation must confirm exact `stopped` before official MA or HA restoration can
proceed. A successful stop response cannot substitute for that observation. Unknown/error
states fail closed and produce a failed receipt, without a Supervisor restart, cached-state
bypass, or extra recovery budget. This shared predicate also rejects uncertain official app
state before a start request. Existing failed historical receipts remain failed; this offline
change is not live recovery proof. Recovery reloads its existing HA config entry only when
this controller started the app. This resets HA's reconnect backoff without changing the
entry's configuration or credentials or starting
any discovery flow. It waits for the selected targets, restores groups/levels/play state,
then verifies them before writing its
owner-only receipt. A group join accepted during startup can precede effective group
readiness. Verification re-reads membership and reconciles a still-missing captured group
at five-second intervals, inside the SAME absolute 90-second recovery deadline. It does
not extend that budget, retry volume commands, or promote receiver telemetry into control.
Before every group mutation and during verification it rejects groups containing an
uncaptured player or malformed membership. A matching response read after the deadline
also remains failure. A missing or `verified: false` receipt is a failed recovery -- do not
claim success. The one-shot reconciliation gap is reproduced offline; the precise phase
that exhausted the original live window remains unproven. The former future grouped
cold-recovery proof requirement is retired, NOT completed, under the main-only hardware
constraint above. A process-held application-wide lease prevents overlapping watchdogs for
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

The private aggregate capture integration's latest local gate passes canonical pre-commit
including mypy, changed-file `ty`, and 22,524 tests with 23 existing skips and 76 snapshots.
Real child/request regressions cover buffered and late-reader stderr cleanup, stdout ACK
revocation before/after stderr EOF and during graceful stop, and finite malformed numeric
DACP handling. Explicit force-stop and actual SIGKILL escalation invalidate records even
after detachment. The FFmpeg test doubles now model optional stderr and use restored
monkeypatches instead of assignment waivers. These are private collector tests, not native
output-role binding, interval coverage or hardware feature acceptance. No new HAOS source
or asset is authorized by these local results.

The readiness hardening gate passes canonical pre-commit/mypy, changed-file `ty`, and
22,570 tests with 23 existing skips and 76 snapshots. An earlier full run hit one
player-grouping lock-order timeout; the unchanged 13-test class and the subsequent full
suite passed. The earlier 600-second interrupted run is not a pass. Receipt privacy tests
compare exact structures rather than rejecting arbitrary PID digit substrings that can
legitimately appear in public digest words. Exact-commit Linux CI remains required.
