# noise-collector

A continuously running Python collector for a Raspberry Pi 4B with a **miniDSP UMIK-1** USB
measurement microphone (built-in preset; see `docs/umik1.md`). It measures sound locally and detects candidate
disturbances. It keeps event audio with 10 s of pre-roll and 30 s of post-roll, and it delivers
readings and recordings reliably to the Laravel application.

This is a **DIY monitoring instrument**. It does not claim Class 1/Class 2 conformance or any
accuracy rating beyond what testing establishes. It does not identify vehicles or sources, judge
regulatory violations, or estimate speed. Events are *candidate disturbances* for the owner to
label in Laravel.

## Status

| Area | State |
|---|---|
| Replay harness, DSP, timing, detector, evidence, SQLite state, delivery, config, CLI | Implemented and tested on a development machine (`pytest`: unit, integration, fault injection, contract) |
| Wire contract | Reconciled with the web app (`my-neighbor-sucks`, vendored in `contract/upstream`). Payloads are schema-validated. A live end-to-end run against the local Laravel stack passed. Server-side gaps are listed in `contract/README.md`. |
| Live ALSA capture | Implemented and tested with a simulated PortAudio stream. **Not yet run on a Pi with a real microphone.** |
| Calibration | dBFS collection works. SPL needs an estimated or calibrated `[calibration]` in `collector.toml` (reported to the web app by the collector itself); `calibrated` needs a passing `calibration-check`. **No physical calibration has been done.** |
| Hardware acceptance (72 h soak, outage, unplug/replug, timing, calibration) | **Pending.** Procedures are in `docs/hardware-validation.md`. |
| Optional third-octave bands, LCpeak | Deliberately disabled until validated (reported honestly in capabilities) |

## Quick start (development machine)

```bash
python3.11 -m venv .venv && .venv/bin/pip install -r requirements-dev.lock && .venv/bin/pip install -e . --no-deps
.venv/bin/pytest                                         # full suite (~40 s)
.venv/bin/noise-collector synth mixed /tmp/mixed.wav     # SYNTHETIC engine-like + garage-door-like + impulse
.venv/bin/noise-collector replay /tmp/mixed.wav --state-dir /tmp/replay
.venv/bin/python scripts/bench_dsp.py --seconds 300      # CPU / RSS of the engine
```

Replay runs the real engine and durability layer: per-second measurements, events, and finalized
WAV segments land in `/tmp/replay/collector.db` and `/tmp/replay/recordings/`.

## Install on the Pi (native systemd, primary)

See `docs/operations.md`. In short: `sudo deploy/install.sh`, put the device token in
`/etc/noise-collector/credentials.toml` (mode 0600), run `noise-collector devices`, then
`provision`, then `doctor`, then `systemctl enable --now noise-collector`. A Docker/Compose
packaging exists in `deploy/docker/` as an optional mode. An optional local live dashboard (read-only
per-second levels, events and health; never audio) is described in `docs/dashboard.md`.

## Layout

```
src/noise_collector/
  audio/        PCM formats, ALSA capture (callback copies only), file replay, discovery, gain readback, buffers
  dsp/          committed A/C/LF coefficients, design + independent references, calibration/correction, metrics
  timing/       sample->monotonic->UTC mapping, drift, step/jump detection, clock sync status
  detect/       percentile baseline, deterministic event state machine
  evidence/     crash-safe PCM chunk spool, immutable WAV finalization, recovery
  store/        SQLite (WAL, FULL sync), versioned migrations, instance locks
  acquisition/  engine (single-threaded, deterministic), durability worker, live runner, replay, recovery
  delivery/     outbox batches, config staging, control lane + audio lane, retention
  transport/    API client + outcome classification, storage uploader, backoff/rate limiting
  contract/     wire models (proposed v1), configuration/profile documents
  dashboard/    optional local live view (stdlib HTTP + SSE, self-contained page)
  supervisor.py process supervision + systemd watchdog; cli.py
contract/       OpenAPI, generated JSON Schemas, deterministic fixtures, open items
deploy/         systemd unit, installer, example settings, Dockerfile + Compose
docs/           architecture, metrics, detection, calibration, operations, hardware validation, limitations
scripts/        filter design (reproducible), contract export, DSP benchmark
tests/          unit, integration (replay, delivery faults, durability kill points, live runner), contract, hardware, laravel
```

Dependencies are pinned with hashes in `requirements.lock`. It was verified to resolve to
CPython 3.11 manylinux aarch64 wheels, as on Raspberry Pi OS Bookworm.
