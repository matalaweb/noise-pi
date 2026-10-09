# miniDSP UMIK-1 setup

The collector has a built-in UMIK-1 preset (`[microphone] model = "umik-1"`). This page explains
what it assumes, why, and how to set up the microphone and its calibration end to end. Facts are
marked **(confirmed)** when seen in primary or concrete device output, and **(convention)** when
they rest on secondary sources. Everything here still has to be confirmed on the purchased unit
(gate H1 in `docs/hardware-validation.md`).

## What the device looks like on Linux

| Item | Value | Basis |
|---|---|---|
| USB id | `2752:0007`, manufacturer `miniDSP` | confirmed (lsusb/dmesg captures) |
| Product string | `Umik-1  Gain: 18dB` (two spaces). The number is the internal analog gain: 18 dB on current units, 12 or 0 dB on older ones. | confirmed |
| ALSA card id | Derived from the product string by the kernel (`U18dB`, `U0dB`, ...). **Never used for matching.** | confirmed (sound/core/init.c) |
| USB serial | A placeholder (`000-0000`, or `1`) on every unit | confirmed |
| Real serial | The 7-digit number on the body and in the calibration file (`SERNO`) | confirmed |
| Format | UAC1, **S24_3LE, 48 kHz only**, synchronous endpoint | confirmed (descriptor) |
| Channels | **2 on most units** (same signal on both); **some units are mono** | confirmed (both kinds seen) |
| Mixer | `Mic` capture volume + switch. On USB-C units, 0..127 = −63.5..**0.00 dB** (a real digital gain). No automatic gain control. | corroborated |
| Calibration files | `<serial>.txt` (0°, pointing at the source) and `<serial>_90deg.txt` (90°, pointing up). First line e.g. `"Sens Factor =-0.837dB, AGain =18dB, SERNO: 7103946"` (`AGain` absent in older files), then `Hz dB` lines from about 10 Hz. The curve is the microphone's response; correction subtracts it. | confirmed |

## What the preset does

* **Identity.** It matches `2752:0007` with a `Umik-1` product string. The USB serial is ignored,
  because it is a placeholder. The physical port is pinned with `usb_path`. Without a `usb_path`,
  the collector uses the UMIK-1 only if exactly one is connected; with two it refuses. The real
  microphone identity is the **calibration file's `SERNO`, which must equal the web-app profile's
  microphone serial**. Otherwise the configuration is rejected.
* **Format.** It captures S24_3LE at the native channel count (`[capture] channels`, usually 2) and
  analyses channel 0. The channels are compared on every block and never averaged; a mismatch shows
  in the status file (`channel_mismatch_blocks`). A wrong channel count is reported as
  `format_mismatch` instead of opening the device.
* **Gain.** It reads the mixer back every 30 s. `Mic` must be at the **0.00 dB step and on**. The
  product string's analog gain must equal the calibration file's `AGain`. If either check fails,
  SPL fields go out `null` with `null_reasons: gain_mismatch` and the flag `invalid_calibration`
  under the same profile; dBFS continues.
* **Response correction.** It applies the serial-specific file attached to the calibration record in
  the web app. The file is downloaded with the device token and SHA-256-verified. The curve is
  normalized to 0 dB at 1 kHz and realized as a bounded minimum-phase FIR. With both files
  attached, `calibration_orientation` picks 0° or 90°.

## Setup, step by step

1. **OS.** Use Raspberry Pi OS **Lite**. On a desktop image, PipeWire/PulseAudio can hold the
   microphone, so opening `hw:` fails with EBUSY. Either remove them or disable the UMIK-1 node in
   WirePlumber. `noise-collector doctor` warns when they are running.
2. **Plug in and inspect:**
   ```bash
   sudo -u noise-collector noise-collector umik --cal-file 7103946_90deg.txt
   ```
   This prints the card, `usb_path`, analog gain, native channel count, mixer state, the settings to
   use, and the values to enter in the web app. It changes nothing.
3. **Mixer to 0 dB:**
   ```bash
   amixer -c <card> sset Mic 100% unmute   # verify it reads [0.00dB] [on]
   sudo alsactl store                      # persist across reboots
   ```
4. **Local settings** (`/etc/noise-collector/collector.toml`, see `deploy/collector.example.toml`):
   ```toml
   [microphone]
   model = "umik-1"
   usb_path = "1-1.2"                 # from step 2
   calibration_orientation = "90deg"  # match how the microphone is mounted
   [capture]
   container = "int24"
   valid_bits = 24
   channels = 2                       # 1 on mono units (step 2 tells)
   analysis_channel = 0
   ```
5. **Web app.** Download the current calibration files for your serial from miniDSP. Some files
   were corrected around 2021 without a name change, so re-download old copies. Then:
   * **Measurement profile:** microphone model `miniDSP UMIK-1`, **microphone serial = the 7-digit
     serial**, sample rate 48000, low-frequency band 20–125 Hz.
   * **Calibration record:** either *estimated* or *calibrated*.
     - *Estimated:* `Level at 94 dB SPL` = the `sensitivity_dbfs_at_94db` value from step 2.
     - *Calibrated:* a measured value from a 94 dB / 1 kHz acoustic calibrator. Stop the service and
       run `noise-collector calibration-check --level 94`. Its `calibrations_toml_entry_hint` shows
       the measured `sensitivity_dbfs_at_94db`.
     - Set gain configuration to e.g. "analog 18 dB; Mic capture 0.00 dB".
     - **Attach** the calibration file(s) as *Microphone frequency-response file*.
   * Publish a configuration referencing the profile, deployment, and calibration. Then run
     `noise-collector doctor`.

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
* Synchronous endpoint: the sample clock follows the host USB frame clock. The collector maps
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
