# Calibration guide (owner)

## Modes

| Mode | What is published | Requirements |
|---|---|---|
| `uncalibrated` | `rms_dbfs` only. All SPL fields are `null`; detection uses relative dBFS rules. | Default until a scale is established |
| `estimated` | SPL fields labeled by an *estimated* profile | Explicitly provisioned estimated profile, e.g. from a casual comparison with a phone app |
| `calibrated` | SPL fields | An explicit scale from path 1 or 2 below **and** a passing end-to-end check |

The collector owns its measurement chain. It builds the **measurement profile** (microphone,
serial, sample rate, processing versions, supported metrics) and the **calibration record**
(state, sensitivity, reference, frequency-response file) from `collector.toml` and registers both
with the server (`POST /api/v1/device/provenance`, contract/device-reported-provenance.md). The
web app stores them read-only; nothing has to be entered or kept matching there. Each record is
immutable and identified by its content: change anything (a new sensitivity, another file) and the
collector registers a new record and uses it from then on. Historical readings keep the record they
were measured with and are never edited.

## Where the absolute scale and response file come from

Everything is set in `/etc/noise-collector/collector.toml` (`deploy/collector.example.toml`):

```toml
[calibration]
state = "estimated"                    # uncalibrated | estimated | calibrated
frequency_response_file = "/etc/noise-collector/7213485_90deg.txt"
# sensitivity_dbfs_at_94db = -30.082   # required unless derivable (UMIK-1 estimate)
# reference_method = "94 dB / 1 kHz acoustic calibrator"   # required for calibrated
# performed_at = "2026-10-12T15:00:00Z"
```

* **Sensitivity:** `sensitivity_dbfs_at_94db`, the RMS dBFS reading this exact chain (microphone,
  gain setting, ALSA capture level) produces for 94 dB SPL at 1 kHz. The collector derives its
  scale from it: `scale = 20 µPa · 10^(94/20) / 10^(dBFS/20)`. For an *estimated* UMIK-1 chain it
  may be omitted: it is then derived from the file's `Sens Factor` (REW convention, `Sens Factor
  - 30`; see docs/umik1.md).
* **Frequency response:** `frequency_response_file`, the microphone's serial-specific file. Use the
  0° file (`<serial>.txt`) when the microphone points at the source and the 90° file
  (`<serial>_90deg.txt`) when it points up. It is applied as a bounded minimum-phase correction
  normalized to 0 dB at 1 kHz (see `docs/metrics.md`) and reported to the server with its SHA-256.
  `apply_frequency_response = false` reports it without applying it.
* **Microphone serial:** `[microphone] microphone_serial`. For a UMIK-1 it defaults to the file's
  `SERNO`; if both are set and differ, the collector refuses to start measuring (the file belongs to
  another microphone).

`noise-collector doctor` shows the chain and whether it is registered. If the chain is unusable
(for example `estimated` without a sensitivity), acquisition reports `local_config_error` and
measures nothing until it is fixed; the reason is in the status and in `doctor`. After editing,
restart the service.

## What the UMIK-1 calibration file is used for

The serial-specific file (0° or 90°) drives the optional `fir_min_phase_v1` response correction
and is registered with the server verbatim. Its `SERNO` is the microphone serial. Its `Sens Factor`
supports an *estimated* sensitivity only (a secondary-source convention, docs/umik1.md); a
*calibrated* chain needs a measured value from one of the two paths below.

## Absolute scale paths

Both paths end in a `sensitivity_dbfs_at_94db` value for `[calibration]` (`calibration-check`
prints a `collector_toml_calibration_hint` with it).

**Path 1: documented manufacturer sensitivity.** State the expected dBFS reading at 94 dB SPL
(1 kHz) for the exact gain setting and PCM scaling, with the source document. Convert it with
`scale = p0·10^(94/20) / 10^(dBFS/20)`. Confirm the assumption with a reference fixture: record
a reference tone and run `calibration-check --sensitivity-dbfs <value>`. The report shows the
sensitivity-derived scale next to the measured one.

**Path 2: reference acoustic level.** Use an acoustic calibrator in a suitable coupler (for
example 94 dB at 1 kHz), or a side-by-side comparison with a reference sound level meter.

```bash
sudo systemctl stop noise-collector          # the microphone is exclusive
sudo -u noise-collector noise-collector calibration-check --level 94.0 --frequency 1000 --seconds 15
sudo systemctl start noise-collector
```

The check band-limits the signal to one-third octave around the tone, discards the first
second, and computes `scale = p0·10^(L/20) / rms(corrected_x)`. It reports the band and
broadband dBFS, and the difference from the current profile scale. Put the details in
`[calibration]` (`reference_method`, `reference_device`, `performed_at`, `performed_by`, `notes`):
source level, frequency, equipment identity and calibration date, procedure, gain readback, and
orientation. A phone-app comparison supports `estimated` only.

## End-to-end check (required for `calibrated`)

After restarting with the new calibration, run `calibration-check` again with the reference. Record the
result at several levels, and at relevant frequencies, especially low frequencies. Report the
differences; do not claim a tolerance. Repeat the check whenever gain readback changes, after
firmware or OS audio updates, and periodically (field check history).

## Gain changes

Set `[microphone] expected_gain_controls` from the `devices` output. An unexplained gain change
invalidates SPL output: under the same profile, the collector sends `null` SPL fields with
`null_reasons: gain_mismatch` and the flag `invalid_calibration`. The server excludes those values
from absolute summaries. It never relabels data or switches calibration state. A replacement
microphone doesn't match the selector (USB serial or port), so it isn't used until the owner updates
local settings; its new profile and calibration are then registered automatically.
