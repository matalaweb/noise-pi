# Device API contract

**Authoritative source:** the web app's `docs/openapi` (repository `my-neighbor-sucks`), vendored
here unchanged in `upstream/`. `upstream/SOURCE` records the commit. Re-vendor with
`scripts/sync_contract.sh` and review the diff; `tests/contract` fails when the vendored copy
differs from a local checkout of the web app.

## How it is tested

| Level | What | Where |
|---|---|---|
| Schema | Collector payloads from real replays validate against the upstream JSON Schemas: measurement batches, open and finalized event revisions (including data-loss finalization), WAV and FLAC declarations, completions, acknowledgments, heartbeats. Upstream request and response examples parse with the collector's models. | `tests/contract/test_contract.py` |
| Behaviour (fake) | An in-process fake mirrors the Laravel services: envelopes, `error.retry`, batch and row idempotency, provenance checks, terminal events, two-stage verification. It validates every body against the upstream schemas and supports fault injection. | `tests/support/fake_server.py`, `tests/integration/test_delivery.py` |
| Behaviour (real) | Full pipeline against the running web app: config fetch and hash check, engine replay, batches, events, acknowledgment, heartbeat, FLAC upload to RustFS with server-side verification, and idempotent replay. | `scripts/run_live_contract_test.sh`, which runs `tests/integration/test_laravel_live.py` |

The live test passed against the web app at the vendored commit (2026-10-08).

## Integration decisions (collector side)

1. **Configuration hash.** `sha256` is checked against the canonical JSON rules of the web app's
   `App\Support\CanonicalJson` (PHP float spelling verified against PHP 8), after re-typing the
   fields `buildDocument()` casts to float. See server issue 1.
2. **Device-reported measurement chain** (2026-10-09, `device-reported-provenance.md`). The
   collector builds its measurement profile and calibration from `collector.toml` and the local
   frequency-response file, registers them with `POST /api/v1/device/provenance` before sending
   anything that references them, and re-registers on `unknown_provenance`. Readings carry no
   placement: the server assigns the one in effect at capture time. Before any configuration is
   published the collector runs on local defaults and sends `configuration_revision: null`.
   Configuration documents carry operational settings only; a configuration it can't honor is
   rejected with a reason and the previous one stays active (missing channel, third-octave bands,
   an LCpeak trigger).
3. **Absolute scale and response.** Both come from `[calibration]` (docs/calibration.md). Without
   a scale, SPL fields are `null` with `null_reasons: calibration_unavailable` and the flag
   `invalid_calibration`, while dBFS continues.
4. **Detection mapping.**
   - `min_event_duration_ms` sets the number of consecutive qualifying seconds.
   - `merge_gap_ms` sets the quiet seconds needed to end an event; post-roll is raised to at
     least that.
   - `baseline_relative` is the trailing 20th percentile over `baseline_window_seconds`, with a
     minimum of min(120, window/2) eligible seconds.
   - Uncalibrated profiles trigger on `rms_dbfs` only.
   - `observation_period_until` is recorded only (no server semantics yet).
5. **Interrupted events.** The server requires `ended_at` for `finalized`. When observation stops
   during an event, the collector finalizes it at the **last observed second** and sets the flag
   `incomplete_interval`. Causes are flagged too: device loss → `microphone_disconnected`, sample
   loss → `audio_dropout`, process crash → `processing_error`. The flags say that observation
   ended, not that the noise stopped.
6. **Measurements.**
   - Unsupported metrics are sent as `null`. Supported metrics that are null carry `null_reasons`;
     `lcpeak_db` is reported as `unsupported` because it isn't validated.
   - Values are not rounded.
   - Local-only diagnostics (near-full-scale, DC, timestamp fallback) never go on the wire.
   - Intervals with untrusted UTC stay local, as specified, and are not sent as
     `unsynchronized_clock`.
   - Incomplete seconds are omitted.
7. **Retry policy.**
   - `error.retry` drives behaviour: `backoff` means full jitter.
   - `after_clock_sync` holds for 10 minutes.
   - `after_configuration_refresh` refetches configuration and holds for 5 minutes.
   - `after_correction` stops authenticated traffic until the token file changes.
   - `never` quarantines, except `outside_backfill_window` (expired for automatic upload) and
     `payload_too_large` (split, then resend).
8. **Recordings.**
   - WAV (PCM) or FLAC, following `recording.format`. FLAC is decoded and compared sample by
     sample with the raw spool before hashing.
   - The local clip is deleted only after `status: verified` **and** `verified_sha256` equals the
     local hash.
   - `failed` with `sha256_mismatch`, `size_mismatch`, `object_missing`, or `unreadable_media`
     triggers a re-upload, if the local bytes are intact, up to 3 attempts.
   - Media-mismatch failures are quarantined.

## Server-side issues found during integration (fixed in the web app on 2026-10-09)

1. **Configuration `sha256` was not reproducible from the served document.** It was computed on
   the in-memory build (`delta_db` as the float `15.0`), while the JSON column serves `15`. Fixed:
   `DeviceConfigurationService::publish()` now hashes the stored form. The collector accepts the
   plain hash, and still accepts the float-typed hash for revisions published before the fix.
2. **Provenance lacked calibration and identity details.** First fixed by serving them in the
   configuration; superseded on 2026-10-09 by device-reported provenance (decision 2), which
   removed that route and the configuration's `provenance` block.

   Also fixed: `Attachment::$fillable` lacked `uuid`, so attaching any file in the panel failed.
3. **Unknown event end.** The protocol is kept: finalized at the last observed second with
   `incomplete_interval`. The OpenAPI now documents this meaning, and the event list shows
   "≥ duration · observation stopped".
4. **Heartbeat `boot_id`** is now nullable. The collector sends `null` when it has no acquisition
   session.
