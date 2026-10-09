# Operations runbook

## Hardware and OS

* Raspberry Pi 4B, official 5.1 V/3 A supply, heatsink or fan case. Throttling and undervoltage
  flags and the CPU temperature show in `doctor` and the status file.
* Raspberry Pi OS Lite 64-bit (Bookworm, Python 3.11). A USB SSD is preferred for spooling. A
  high-endurance microSD is acceptable at first; watch free space and write volume.
* Time sync: `chrony` (installed by `install.sh`) or `systemd-timesyncd`. Without sync, data is
  captured locally but **not uploaded** (untrusted UTC).
* Microphone: miniDSP UMIK-1 (setup in `docs/umik1.md`). Fixed position and orientation, mounted on something that does not vibrate, away
  from fans and the garage door opener motor where practical. Record placement notes in Laravel.
  A garage measurement describes the garage, not a bedroom or a standardized outdoor position.
  **Moving the microphone requires a new deployment revision.**

## Install / provision

```bash
sudo bash deploy/install.sh
sudoedit /etc/noise-collector/credentials.toml       # device_token = "..."  (never on a command line)
sudo chown noise-collector:noise-collector /etc/noise-collector/credentials.toml && sudo chmod 600 $_
sudo -u noise-collector /opt/noise-collector/venv/bin/noise-collector devices   # USB identity, formats, gain controls
cp deploy/bootstrap.example.toml ~/bootstrap.toml && $EDITOR ~/bootstrap.toml  # base URL, mic selector, expected IDs
sudo -u noise-collector /opt/noise-collector/venv/bin/noise-collector provision --bootstrap ~/bootstrap.toml --output /etc/noise-collector/collector.toml --yes
sudo -u noise-collector /opt/noise-collector/venv/bin/noise-collector doctor
sudo systemctl enable --now noise-collector
```

`provision` writes the non-secret settings and initializes the state directory. It then fetches
the server configuration, checks it against the expected deployment/profile/calibration IDs, and
stages it. The collector never self-registers or invents identities. Without a configuration it
runs diagnostics only.

Gain: copy the `gain_controls` line printed by `devices` into the Laravel profile. Any
automatic gain or processing control must be off. If gain cannot be read back, the profile must
name a repeatable reference check before it can be `calibrated`.

## Everyday commands

| Command | Purpose (read-only unless noted) |
|---|---|
| `noise-collector status [--json]` | Microphone/session/detector state; last captured, stored, acknowledged and heartbeat times; counters |
| `noise-collector inspect-backlog` | Pending/quarantined batches, events, recordings, acks |
| `noise-collector repair-plan` | Items that need an owner decision and the safe options |
| `noise-collector export-diagnostics out.zip [--include-audio]` | Redacted bundle (no tokens, signed URLs or audio unless asked) |
| `noise-collector calibration-check --level 94 [--file ref.wav]` | Scale check against a reference (stop the service for live capture) |
| `noise-collector backup out.db` | Online SQLite backup that includes WAL content; *writes `out.db`* |
| `noise-collector umik [--cal-file F]` | UMIK-1 setup report: device, mixer, calibration file, settings and web-app values (read-only) |
| `noise-collector dashboard` | Local live view of levels and events (read-only, never audio; see `docs/dashboard.md`) |
| `noise-collector shutdown` | Graceful stop of a foreground supervisor (use `systemctl stop` for the service) |
| `journalctl -u noise-collector`, `/var/lib/noise-collector/logs/*.log` | JSON logs with UTC and monotonic time, redacted and rate-limited |

## Health states and what to do

| Symptom (status/heartbeat) | Meaning | Action |
|---|---|---|
| `microphone_state: disconnected` | Device not matched (unplugged, wrong selector, another process holds it) | Check `devices` and cabling. Reconnects automatically (1, 2, 5, 10, 30 s backoff). Each reconnect is a new session and a recorded gap. |
| `format_mismatch` | Device cannot deliver the profile's format | Check `devices` native formats against the profile capture spec |
| `gain_mismatch` | Mixer readback differs from the profile | SPL is withheld, or the provisioned uncalibrated fallback profile is used. Restore the gain or provision a new profile. |
| `durable_capture: critical` | SQLite/disk failing or free space below the metadata floor | Free space or replace the storage. Existing unacknowledged data is preserved. Capture resumes when commits succeed. |
| `auth_blocked: true` | 401/403 from Laravel | Fix the token file; delivery resumes automatically when the token changes. Collection continues locally. |
| Many `local_only` rows with `clock_unsynchronized` | No trusted UTC | `chronyc tracking`; check network/NTP. That data is quarantined from upload. |
| Quarantined batches/recordings | 409/422/corruption | `inspect-backlog`, `repair-plan`. Data is never rewritten to make a request pass. |
| `expired_for_automatic_upload` | Older than the 30-day backfill window | Needs an owner-enabled import in Laravel. Kept locally within quota. |

## Storage

Quotas derive from the volume size unless set: reserve max(1 GiB, 10%), metadata min(4 GiB, 20%),
finalization headroom of 2 × 95 MiB, and the rest for audio. At the warning level, retention
prunes acknowledged rows and verified audio early. Before the audio quota is exhausted, new
recordings are refused (flagged `audio_coverage_loss`) and measurements continue. Pending
originals are never deleted automatically. Defaults: acknowledged measurements are kept 7 days;
server-verified audio is kept 24 h after verification with a matching SHA-256, then deleted with
a receipt. Deleting the local copy does not affect the cloud original.

## Upgrade and rollback

Upgrades are owner-initiated pinned releases. Nothing updates itself.

1. `noise-collector backup /var/lib/noise-collector/backups/pre-upgrade.db`, and copy
   `recordings/` and `spool/` if you want a full snapshot.
2. `sudo bash deploy/install.sh` from the new release. It builds a fresh virtualenv under
   `/opt/noise-collector/releases/<time>/`, repoints the `venv` symlink to it, and keeps the
   prior release as `previous`. Data and config are untouched.
3. `sudo systemctl restart noise-collector`. Migrations run before capture starts, and a
   pre-migration database backup is written to `backups/` automatically.
4. Rollback: `sudo systemctl stop noise-collector`, then
   `sudo ln -sfn "$(readlink /opt/noise-collector/previous)" /opt/noise-collector/venv`. If the
   newer version migrated the schema, also restore the pre-migration backup. An older agent refuses
   to open a newer schema rather than corrupt it.

## Backups

Use `noise-collector backup` (SQLite online backup API) or stop the service and copy the whole
state directory. Never copy only `collector.db` from a running system: the WAL holds committed
data.

## Container mode (optional)

`deploy/docker/compose.yaml` runs the same code with `/dev/snd` bind-mounted, a cgroup rule for
ALSA (major 116), `group_add` of the host audio GID, a read-only root filesystem, all
capabilities dropped, and no published ports. The healthcheck uses the same progress test as the
systemd watchdog.

* The credentials file must be readable by container UID 10001: `chown 10001` on the host file,
  mode 0600.
* Unplug/replug must be re-tested in container mode.
* Confirm with `docker compose exec noise-collector noise-collector doctor` that the clock check
  reports `synchronized`. If the runtime's seccomp policy blocks the read-only `adjtimex`, data
  stays untrusted.
