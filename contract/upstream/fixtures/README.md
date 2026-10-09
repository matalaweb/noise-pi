# Device API fixtures

Sample request bodies for `docs/openapi/device-api-v1.yaml`. Values are
illustrative only and synthetic.

## Placeholders

Request fixtures reference registered records through placeholders that a
client (or `tests/Feature/DeviceApi/OpenApiContractTest.php`) substitutes
before sending:

| Placeholder | Replace with | JSON type after substitution |
| - | - | - |
| `{{BOOT_ID}}` | agent boot UUID | string |
| `{{PROFILE_ID}}` | measurement profile UUID the device registered (`POST /provenance`) | string |
| `{{CALIBRATION_ID}}` | calibration UUID the device registered (use `null` for an uncalibrated profile) | string or null |
| `"{{CONFIGURATION_REVISION}}"` | applied configuration revision — the whole quoted token is replaced by an integer (or `null` on local defaults) | integer or null |
| `{{CONFIGURATION_SHA256}}` | `sha256` from `GET /configuration` | string |
| `{{ATTEMPT_ID}}` | `upload.attempt_id` returned by the recording declaration | string |

Fixed UUIDs (`batch_id`, `event_id`, `recording_id`, and the profile and
calibration ids in `provenance-registration.json`) are kept literal so the
same file can demonstrate idempotent replay. Readings and events carry no
placement: the server assigns the placement in effect at capture time.

`provenance-registration.json` carries a short synthetic UMIK-1-style
frequency-response file inline; its `sha256` matches the decoded bytes.

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
| `provenance-registration.json` | `POST /provenance` (estimated UMIK-1 chain with its frequency-response file) |
| `measurement-batch.json` | `POST /measurements/batches` |
| `measurement-batch-flagged.json` | `POST /measurements/batches` (quality flags, null reasons, bands) |
| `event-open.json` | `POST /events` (revision 1, open) |
| `event-finalized.json` | `POST /events` (revision 2, finalized) |
| `recording-declaration.json` | `POST /events/{event_uuid}/recordings` |
| `recording-completion.json` | `POST /recordings/{recording_uuid}/complete` |
| `heartbeat.json` | `POST /heartbeat` |
| `configuration-acknowledgment.json` | `POST /configuration/acknowledgments` |
| `responses/*.json` | Representative responses (identifiers illustrative) |
