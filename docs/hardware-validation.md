# Hardware validation (pending)

None of these checks has been run yet: no Pi, microphone, or reference equipment was available
to the implementing agent. Synthetic tests do not replace them. Record results in
`docs/validation/` with the date, hardware serials, OS/kernel, agent version, and configuration
revision.

## Gate checklist

| # | Check | How | Pass evidence |
|---|---|---|---|
| H1 | Device identity and format (UMIK-1) | `noise-collector umik --cal-file <file>`; `noise-collector doctor`; `pytest -m hardware tests/hardware` | `2752:0007` matched by `usb_path`; S24_3LE at 48 kHz with the native channel count (record 1 or 2); `channel_mismatch_blocks` stays 0 over 1 h (record if not); `Mic` at 0.00 dB and on after reboot (`alsactl store`); product-string gain equals the calibration file's `AGain`; file `SERNO` equals the body serial |
| H2 | ADC timestamp behavior | Hardware test prints the timestamp source and callback offset spread; status shows `stream_to_mono_offset_s` | `adc` source (or documented fallback); offset spread recorded |
| H3 | Sustained capture | `scripts/bench_dsp.py --seconds 600` with and without `--correction`, then a 1 h live run | No `overflow_frames` or `driver_overflows`; CPU and RSS within targets (< 1 core average, < 512 MiB) |
| H4 | 72 h soak | Continuous run with realistic bursts (playback of `noise-collector synth mixed` through a speaker, plus real activity), uploads enabled | Zero application-induced dropouts; record CPU/RAM, temperature/throttling, disk growth, upload bandwidth, missing seconds |
| H5 | 24 h outage | Block the Laravel host at the router for 24 h, then restore | All in-window data is acknowledged; no duplicates (server counts = local counts); recordings verified; backlog recovery speed recorded |
| H6 | Unplug/replug | Repeat 20× in native mode, and again in container mode if used | Each reconnect creates a new session and a recorded gap; identity re-verified; no capture restart loop; uploader unaffected |
| H7 | Process kills and reboots | `kill -9` the acquisition child during an event; pull power during an event (SSD and SD separately) | Recovery finalizes incomplete segments with no fabricated samples; no committed measurement lost; behavior documented per storage device |
| H8 | Uncertain clock | Boot with the network disconnected and the clock wrong, then connect | Data stays `local_only` until sync; any clock step creates a new epoch/session; no invented history uploaded |
| H9a | UMIK-1 sensitivity convention | Calibrator at 94 dB / 1 kHz with `calibration-check`, compared with `noise-collector umik`'s estimate (Sens Factor − 30 dBFS) | Report the difference in dB. The record stays *estimated* until a calibrator value is entered. |
| H9 | Scale and response | Calibrator at 94/114 dB at 1 kHz, plus a reference meter comparison at several levels and at 31.5, 63, 125, 250 Hz and 4, 8 kHz | Report differences per level and frequency; no invented accuracy class |
| H10 | Timing alignment | Make a sharp acoustic event (clap or starter pistol) while a GPS-disciplined or NTP-verified reference records its time; compare with the measurement second and the recording sample offset | Report the measured error; must be < 100 ms, otherwise trusted timing stays suspended |
| H11 | Garage-door labeling | Operate the owner's garage door under control | Event appears in Laravel with audio, and can be labeled manually |
| H12 | Final demo | Real event, pre/post-roll audio, internet outage, upload without duplicates, server verification, measurements/event/recording visible in Laravel; USB interruption and uncertain-clock startup visible as gaps | Screenshots and the diagnostics bundle |

## Notes

* Measure burst and finalization load separately: watch CPU while a 10-minute segment
  finalizes (SHA-256 of about 86 MB plus the copy).
* On microSD, record total bytes written per day (`/sys/block/mmcblk0/stat`) to estimate wear.
* If ADC timestamps are unavailable (`fallback`), add the measured fixed latency to
  `capture.fallback_latency_ms` and record the residual uncertainty.
