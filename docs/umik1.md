# miniDSP UMIK-1 setup

The collector has a built-in UMIK-1 preset (`[microphone] model = "umik-1"`). This page explains
what it assumes, why, and how to set up the microphone and its calibration end to end. Facts are
marked **(confirmed)** when seen in primary or concrete device output, and **(convention)** when
they rest on secondary sources. Everything here still has to be confirmed on the purchased unit
(gate H1 in `docs/hardware-validation.md`).

## What the device looks like on Linux

| Item | Value | Basis |
|---|---|---|
| USB id | `2752:0007`, manufacturer `miniDSP` (older) or `miniDSP Ltd.` (newer) | confirmed (lsusb/dmesg captures; our unit) |
| Product string | Older units: `Umik-1  Gain: 18dB` (two spaces; the number is the internal analog gain, 18, 12 or 0 dB). **Newer revision (bcdDevice 1.23): just `UMIK-1`, no gain.** | confirmed (our unit, 2026-10-09) |
| ALSA card id | Derived from the product string by the kernel (`U18dB`, `U0dB`, `UMIK1`, ...). **Never used for matching.** | confirmed (sound/core/init.c) |
| USB serial | A placeholder (`000-0000`, or `1`) on every unit | confirmed |
| Real serial | The 7-digit number on the body and in the calibration file (`SERNO`) | confirmed |
| Format | UAC1, **S24_3LE, 48 kHz only**. Older units: synchronous endpoint; newer revision: asynchronous | confirmed (descriptors; our unit) |
| Channels | **2 on older units** (same signal on both); **mono on others, including the newer revision** | confirmed (both kinds seen; ours is mono) |
| Mixer | `Mic` capture volume + switch. On USB-C units, 0..127 = −63.5..**0.00 dB** (a real digital gain). No automatic gain control. | corroborated |
| Calibration files | `<serial>.txt` (0°, pointing at the source) and `<serial>_90deg.txt` (90°, pointing up). First line e.g. `"Sens Factor =-0.837dB, AGain =18dB, SERNO: 7103946"` (`AGain` absent in older files), then `Hz dB` lines from about 10 Hz. The curve is the microphone's response; correction subtracts it. | confirmed |

## What the preset does

* **Identity.** It matches `2752:0007` with a `Umik-1` product string. The USB serial is ignored,
  because it is a placeholder. The physical port is pinned with `usb_path`. Without a `usb_path`,
  the collector uses the UMIK-1 only if exactly one is connected; with two it refuses. The real
  microphone identity is the **calibration file's `SERNO`**, reported as the profile's microphone
  serial. If `[microphone] microphone_serial` is also set and differs, the collector refuses the
  file (it belongs to another microphone).
* **Format.** It captures S24_3LE at the native channel count (`[capture] channels`, usually 2) and
  analyses channel 0. The channels are compared on every block and never averaged; a mismatch shows
  in the status file (`channel_mismatch_blocks`). A wrong channel count is reported as
  `format_mismatch` instead of opening the device.
* **Gain.** It reads the mixer back every 30 s. `Mic` must be at the **0.00 dB step and on**. The
  product string's analog gain must equal the calibration file's `AGain` (a newer-revision unit does
  not report its gain; that is noted in the status, not failed). If either check fails,
  SPL fields go out `null` with `null_reasons: gain_mismatch` and the flag `invalid_calibration`
  under the same profile; dBFS continues.
* **Response correction.** It applies `[calibration] frequency_response_file` (the 0° or the 90°
  file, matching how the microphone is mounted). The curve is normalized to 0 dB at 1 kHz and
  realized as a bounded minimum-phase FIR. The file is registered with the server with its SHA-256.
* **Profile and calibration.** The collector reports both to the server itself (model
  `miniDSP UMIK-1`, the file's serial, an *estimated* sensitivity of `Sens Factor - 30` unless
  `sensitivity_dbfs_at_94db` is set). Nothing is entered in the web app except the placement.

## Setup, step by step

1. **OS.** Use Raspberry Pi OS **Lite**. On a desktop image, PipeWire/PulseAudio can hold the
   microphone, so opening `hw:` fails with EBUSY. Either remove them or disable the UMIK-1 node in
   WirePlumber. `noise-collector doctor` warns when they are running.
2. **Plug in and inspect:**
   ```bash
   sudo -u noise-collector noise-collector umik --cal-file 7103946_90deg.txt
   ```
   This prints the card, `usb_path`, analog gain, native channel count, mixer state and the
   `collector.toml` settings to use. It changes nothing.
3. **Mixer to 0 dB:**
   ```bash
   amixer -c <card> sset Mic 100% unmute   # verify it reads [0.00dB] [on]
   sudo alsactl store                      # persist across reboots
   ```
4. **Calibration file.** Download the current calibration files for your serial from miniDSP (some
   files were corrected around 2021 without a name change, so re-download old copies) and copy the
   one matching the mounting to `/etc/noise-collector/`.
5. **Local settings** (`/etc/noise-collector/collector.toml`, see `deploy/collector.example.toml`):
   ```toml
   [microphone]
   model = "umik-1"
   usb_path = "1-1.2"                 # from step 2
   [capture]
   container = "int24"
   valid_bits = 24
   channels = 1                       # native count from step 2 (older units: 2)
   analysis_channel = 0
   [calibration]
   state = "estimated"
   frequency_response_file = "/etc/noise-collector/7213485_90deg.txt"
   ```
   Restart the service and run `noise-collector doctor`: it shows the measurement chain (model,
   serial, estimated sensitivity) and its registration with the server. The collector measures
   right away on local defaults (measurements only, no detection rules).
6. **Web app.** Add a **placement** for the device (where the microphone is). Readings are assigned
   the placement in effect at their capture time. Optionally publish a configuration for detection
   rules, recording and intervals; profile and calibration need no entry.
7. **Calibrated later:** with a 94 dB / 1 kHz acoustic calibrator, stop the service, run
   `noise-collector calibration-check --level 94`, and copy its `collector_toml_calibration_hint`
   into `[calibration]` (state `calibrated`, the measured sensitivity, reference method, date).

## Sensitivity: what is and isn't known

miniDSP publishes no absolute sensitivity figure for the UMIK-1. The `Sens Factor` in the file
header is interpreted by REW as follows (convention, from the REW author's posts):

* `Sens Factor` is the dBFS reading for 100 dB SPL at the old +24 dB Windows input setting.
* So full-scale SPL is `124 − Sens Factor` at 0 dB digital gain.
* So **94 dB SPL reads `Sens Factor − 30` dBFS RMS** (full-scale sine = −3.01 dBFS).

REW's own dialog ("Full scale SPL = 124.84 dB" for −0.837) and one published calibrator check
(0.44 dB difference) support it. The often-quoted "−18 dBFS at 94 dB" is **not** supported; it is
about 12 dB off. There are also conflicting statements about the clip point at 18 dB gain (about
115 dB vs about 124 dB SPL).

So the collector never derives the scale from the header by itself. `noise-collector umik` turns it
into a suggested *estimated* calibration record, and a calibrator check (or a reference-meter
comparison) is what justifies a *calibrated* record. Expect around ±1 dB unit-to-unit agreement
with the convention, and record the measured difference.

## Known Linux pitfalls

* EBUSY on open: an audio server holds the device (see step 1). `lsof /dev/snd/*` shows who.
* `hw:` can't convert format, rate, or channel count. The configured format must be native,
  which the collector checks.
* Synchronous endpoint (older units): the sample clock follows the host USB frame clock; newer units
  use an asynchronous endpoint with their own clock. The collector maps
  samples to UTC with drift tracking, never by sample count alone.

## Sources

* lsusb/ALSA captures: https://github.com/ela2342/mugge/blob/main/docs/lab-notes/US-000-installation.md ,
  https://github.com/linuxhw/LsUSB , https://codeberg.org/pander/umik-1
* Placeholder serial, REW serial handling: https://www.avnirvana.com/threads/rew-closes-unexpectedly-when-umik-1-mic-plugged-in.9833/ ,
  https://github.com/gentnerlab/pyoperant/blob/master/scripts/sync_mic_cal.py , https://www.roomeqwizard.com/changehistory.html
* Mixer on Pi: https://github.com/dpwe/noise_monitor (tests/test_alsa.py), https://github.com/danielfcollier/py-umik-base-app/issues/43
* Calibration files and Sens Factor: https://www.roomeqwizard.com/help/help_en-GB/html/calfiles.html ,
  https://www.avnirvana.com/threads/umik-1-sensitivity.10637 , https://www.avnirvana.com/threads/new-umik-1-calibration-files.9308/ ,
  https://www.minidsp.com/images/documents/Product%20Brief%20-%20Umik.pdf
* PipeWire/PulseAudio: https://www.avnirvana.com/threads/linux-debian-like-rew-umik-1-well-recognized.10537/
