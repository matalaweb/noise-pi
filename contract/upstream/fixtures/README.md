# Device API fixtures

Sample request bodies for `docs/openapi/device-api-v1.yaml`. Values are
illustrative only and synthetic.

## Placeholders

Request fixtures reference provisioned records through placeholders that a
client (or `tests/Feature/DeviceApi/OpenApiContractTest.php`) substitutes
before sending:

| Placeholder | Replace with | JSON type after substitution |
| - | - | - |
| `{{BOOT_ID}}` | agent boot UUID | string |
| `{{DEPLOYMENT_ID}}` | placement (deployment) UUID from `GET /configuration` → `provenance.deployments[].id` | string |
| `{{PROFILE_ID}}` | measurement profile UUID | string |
| `{{CALIBRATION_ID}}` | calibration UUID (use `null` for an uncalibrated profile) | string or null |
| `"{{CONFIGURATION_REVISION}}"` | configuration revision — the whole quoted token is replaced by an integer | integer |
| `{{CONFIGURATION_SHA256}}` | `sha256` from `GET /configuration` | string |
| `{{ATTEMPT_ID}}` | `upload.attempt_id` returned by the recording declaration | string |

Fixed UUIDs (`batch_id`, `event_id`, `recording_id`) are kept literal so the
same file can demonstrate idempotent replay.

Timestamps assume the server receives the requests around
`2026-10-08T12:20:00Z`: captures more than 5 minutes in the future or more
than 30 days in the past are rejected.

`measurement-batch-flagged.json` uses third-octave bands (31.5, 63, 125 Hz), so
the referenced profile must define those band centres.

The recording declaration's `sha256` and `byte_size` describe a hypothetical
file; verification of a real upload will fail unless the uploaded bytes match.

## Files

| File | Endpoint |
| - | - |
| `measurement-batch.json` | `POST /measurements/batches` |
| `measurement-batch-flagged.json` | `POST /measurements/batches` (quality flags, null reasons, bands) |
| `event-open.json` | `POST /events` (revision 1, open) |
| `event-finalized.json` | `POST /events` (revision 2, finalized) |
| `recording-declaration.json` | `POST /events/{event_uuid}/recordings` |
| `recording-completion.json` | `POST /recordings/{recording_uuid}/complete` |
| `heartbeat.json` | `POST /heartbeat` |
| `configuration-acknowledgment.json` | `POST /configuration/acknowledgments` |
| `responses/*.json` | Representative responses (identifiers illustrative) |
