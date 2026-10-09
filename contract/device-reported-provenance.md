# Device-reported measurement chain (contract change, 2026-10-09)

The device (the Pi) is the source of truth for its measurement chain: the measurement profile
(microphone, serial, processing) and the calibration (state, sensitivity, frequency-response file).
It registers them with the server; the server stores them as immutable, device-sourced records. The
owner no longer creates profiles or calibrations, and configurations no longer reference them, so
nothing has to be kept "matching" by hand.

Placements (deployments) stay owner-entered in the web app. The device never sends a placement: the
server assigns each reading the placement in effect at its capture time.

The device measures without any published configuration, using local defaults. A published
configuration only carries operational settings.

All JSON follows the existing device API conventions (RFC 3339 UTC with `Z`, UUIDs lowercase,
`schema_version: 1`, `error.retry` semantics, request ids, payload limits).

## 1. New endpoint: `POST /api/v1/device/provenance`

Ability: `measurements:write`. Throttle: `device-control`. Payload limit: 2 MiB.

Request:

```json
{
  "schema_version": 1,
  "sent_at": "2026-10-09T15:00:00.000Z",
  "measurement_profiles": [
    {
      "id": "019a....",                       // device-generated UUID, globally unique
      "channel": "mic-1",
      "name": null,
      "microphone_model": "miniDSP UMIK-1",
      "microphone_serial": "7213485",
      "audio_interface": "USB (built-in)",
      "sample_rate_hz": 48000,
      "gain_db": null,
      "gain_description": "analog gain not reported; ALSA Mic 0.00 dB",
      "weighting_implementation_version": "noise-collector A/C v1 ...",
      "filter_implementation_version": "noise-collector LF v1 ...",
      "agent_processing_version": "noise-collector 0.2.0",
      "calibration_state": "estimated",      // uncalibrated | estimated | calibrated
      "calibration_application_method": "...",
      "supported_metrics": ["laeq_db", "lafmax_db", "lceq_db", "low_frequency_leq_db", "rms_dbfs"],
      "low_frequency_lower_hz": 20,
      "low_frequency_upper_hz": 125,
      "band_centers_hz": []
    }
  ],
  "calibrations": [
    {
      "id": "019a....",
      "channel": "mic-1",
      "calibration_state": "estimated",      // estimated | calibrated (never uncalibrated)
      "reference_method": "UMIK-1 calibration file Sens Factor (REW convention)",
      "reference_device": null,
      "reference_level_db": 94,
      "reference_frequency_hz": 1000,
      "sensitivity_mv_per_pa": null,
      "sensitivity_dbfs_at_94db": -30.082,
      "gain_configuration": "analog gain not reported; Mic capture 0.00 dB",
      "application_method": "sensitivity offset dBFS->SPL; min-phase FIR response correction",
      "performed_at": null,
      "performed_by": null,
      "notes": null,
      "attachments": [
        {
          "purpose": "frequency_response",
          "filename": "7213485_90deg.txt",
          "media_type": "text/plain",
          "sha256": "<hex of the decoded bytes>",
          "content_base64": "<file bytes, base64>"
        }
      ]
    }
  ]
}
```

Rules:

* Either array may be empty (not both). At most 8 items per array; at most 4 attachments per
  calibration, each at most 1 MiB decoded; `sha256` must match the decoded bytes.
* The same validation as the former owner forms applies to the values (uncalibrated profiles only
  support `rms_dbfs`; calibrations are never `uncalibrated`; channel regex `^[A-Za-z0-9._-]{1,32}$`;
  metrics from the Metric enum; sample rate positive).
* Records are immutable. The server computes the content hash over the record (excluding `id`,
  including attachment `sha256`/`filename`/`purpose`, not the bytes) with CanonicalJson.
  * `id` unknown → create (source `device`, `created_by` null, next per-channel revision), store
    attachments, audit `device.profile.registered` / `device.calibration.registered`.
  * `id` already registered for this device with the same content hash → no-op (idempotent).
  * `id` registered with a different content hash, or `id` belongs to another device → `409
    conflict` (`provenance_conflict`, retry `after_correction`), nothing stored for the request.
* The whole request is one transaction.

Response `200`:

```json
{
  "request_id": "...",
  "server_received_at": "...",
  "measurement_profiles": [{"id": "019a...", "revision": 3, "status": "created"}],
  "calibrations": [{"id": "019a...", "revision": 2, "status": "existing"}]
}
```

## 2. Measurement records and event revisions

* `deployment_id`: **optional and ignored** (accepted so older agents still validate). The server
  resolves the placement: the device's deployment with the greatest `effective_at <= captured_at`
  (events: `started_at`), or none.
* `profile_id` / `calibration_id`: must reference records registered for this device (owner-created
  legacy records remain valid). Unknown → `unknown_provenance` (retry
  `after_configuration_refresh`, unchanged); the agent re-registers its provenance, then resubmits.
* `configuration_revision`: `integer >= 1` **or `null`** (`null` = the device ran on local
  defaults, no server configuration applied). A non-null revision must still exist for the device.
* Removed checks: "capture time precedes the deployment effective time".
* Everything else (metric/profile consistency, calibration state/channel consistency) is unchanged.

## 3. Measurement streams

`measurement_streams.device_deployment_id` becomes nullable; the stream key includes the (possibly
null) deployment. UI and exports show "placement not recorded" for a null placement.
`measurements.configuration_revision` and `noise_events.configuration_revision` become nullable.

## 4. Configuration document

`channels[]` items carry only `channel`, `enabled`, `metrics`, `bands_enabled`. The keys
`measurement_profile_id`, `deployment_id`, `calibration_id`, `calibration_state` are no longer
written (old revisions keep them; agents ignore them). `GET /configuration` no longer returns
`provenance` (agents ignore it if present). Server-side publish validation checks channels/metrics
only against the device's reported capabilities (heartbeat), not against profiles.

`GET /api/v1/device/calibrations/{calibration}/attachments/{attachment}` is removed.

## 5. Agent behaviour (noise-pi)

* Builds profile + calibration from `collector.toml` (`[microphone]`, `[calibration]`) and the local
  frequency-response file; for a UMIK-1 the serial defaults to the file's `SERNO` and an
  *estimated* sensitivity to `Sens Factor - 30`.
* Assigns a UUIDv7 per distinct record content (stored locally), registers before sending any
  measurement/event that references it, and re-registers on `unknown_provenance`.
* Runs on local defaults (reporting 30 s, heartbeat 60 s, no detection rules, recording per local
  settings with 10 s / 30 s pre/post-roll) until a configuration is published;
  `configuration_revision` is `null` in that mode.
