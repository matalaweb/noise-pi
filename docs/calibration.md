# Calibration guide (owner)

## Modes

| Mode | What is published | Requirements |
|---|---|---|
| `uncalibrated` | `rms_dbfs` only. All SPL fields are `null`; detection uses relative dBFS rules. | Default until a scale is established |
| `estimated` | SPL fields labeled by an *estimated* profile | Explicitly provisioned estimated profile, e.g. from a casual comparison with a phone app |
| `calibrated` | SPL fields | An explicit scale from path 1 or 2 below **and** a passing end-to-end check |

Calibration state, the profile, and the calibration record all come from the web app and are
immutable. A new calibration means a new calibration record (and, if needed, a new profile) plus a
new configuration revision. Historical readings are never edited. A calibration file existing on
disk never promotes a profile.

## Where the absolute scale and response file come from

Everything is entered once in the web app, on the device's **calibration record**:

* **Sensitivity:** enter `Level at 94 dB SPL` (`sensitivity_dbfs_at_94db`), the RMS dBFS reading
  this exact chain (microphone, gain setting, ALSA capture level) produces for 94 dB SPL at the
  reference frequency. The collector derives its scale from it:
  `scale = 20 µPa · 10^(94/20) / 10^(dBFS/20)`.
* **Frequency response:** attach the microphone's serial-specific file with the purpose
  *Microphone frequency-response file*. The collector downloads it with its device token,
  verifies the SHA-256 listed in the configuration, checks that the file's `SERNO` matches the
  profile's microphone serial, and applies it as a bounded minimum-phase correction normalized
  to 0 dB at 1 kHz (see `docs/metrics.md`). If both 0° and 90° files are attached, set
  `[microphone] calibration_orientation` to choose one. To opt out of correction, set the
  calibration's correction metadata `apply_frequency_response = false`.
* **Microphone serial:** enter it on the measurement profile. The collector refuses a calibration
  file for a different serial.

Adding a file later doesn't change the configuration; the collector picks it up on its next
configuration application. A new calibration means a new calibration record, so old scales and
files are never applied to it.

`/etc/noise-collector/calibrations.toml` (`deploy/calibrations.example.toml`) remains as a
fallback for web-app versions that don't serve sensitivity. It is pinned to the calibration's
`content_hash`, and the server value wins when both exist. Without either, a calibrated or
estimated channel reports `null` SPL fields with `null_reasons: calibration_unavailable` and the
quality flag `invalid_calibration`, plus dBFS.

## What the UMIK-1 calibration file is used for

The serial-specific file (on-axis or 90°) is retained verbatim with its SHA-256. Its frequency
curve can drive the optional `fir_min_phase_v1` response correction (see `docs/metrics.md`).
Attach it to the calibration record in the web app (see above). Its header (for example `Sens Factor`, `AGain`) is kept as provenance text only. **The collector
does not turn the header into a sensitivity formula.** The meaning of those numbers for this
ALSA pipeline is not established. Use one of the two paths below.

## Absolute scale paths

Both paths end in a `sensitivity_dbfs_at_94db` value, entered on the web app's calibration
record as *Level at 94 dB SPL*.

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
broadband dBFS, and the difference from the current profile scale. Store these details in the
Laravel profile: source level, frequency, equipment identity and calibration date, procedure,
date, gain readback, and orientation. A phone-app comparison supports `estimated` only.

## End-to-end check (required for `calibrated`)

After provisioning the new profile, run `calibration-check` again with the reference. Record the
result at several levels, and at relevant frequencies, especially low frequencies. Report the
differences; do not claim a tolerance. Repeat the check whenever gain readback changes, after
firmware or OS audio updates, and periodically (field check history).

## Gain changes

Set `[microphone] expected_gain_controls` from the `devices` output. An unexplained gain change
invalidates SPL output: under the same profile, the collector sends `null` SPL fields with
`null_reasons: gain_mismatch` and the flag `invalid_calibration`. The server excludes those values
from absolute summaries. It never relabels data or switches calibration state. A replacement
microphone (different USB serial) doesn't match the selector, so it isn't used until the owner
updates local settings and the web app has a new profile and calibration.
