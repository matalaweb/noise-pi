# Known limitations and open items

1. **Unknown event end.** The web app requires an end time on finalized events. Events cut short
   by data loss are therefore finalized at the last observed second with `incomplete_interval`,
   and the web app shows "observation stopped".
2. **No hardware validation yet.** Real ALSA timestamps, USB reconnect behavior, gain readback
   on the purchased microphone, CPU and thermal behavior, and storage durability under power
   loss are untested (`docs/hardware-validation.md`).
3. **No physical calibration.** The example and live-test scale (−18 dBFS = 94 dB) is a
   placeholder for synthetic tests and is labeled as such. Real SPL requires path 1 or 2 in
   `docs/calibration.md`.
4. **LCpeak** stays null: no validated transient or bandwidth implementation exists.
5. **Third-octave bands** are not implemented (the `bands` array is always empty). The filter
   bank, validation, and Pi CPU benchmark come first.
6. **Low-frequency trigger** is disabled until field validation
   (`capabilities.low_frequency_validated`).
7. **Pre-roll is held in RAM.** A sudden power loss loses the ring and up to about 1 s of
   unsynced chunk data plus the in-memory backlog.
8. **Baselines reset on every new acquisition session** (reconnect or clock epoch). The detector
   then needs about 2 minutes of warmup before relative rules can trigger.
9. **No maximum event duration.** A permanent level change, such as steady rain above the
   threshold, keeps one long event open. Storage stays bounded by the audio quota and 10-minute
   segments. A configurable cap may be worth adding after field observation.
10. **Untrusted-time data stays local.** Measurements captured before NTP sync, or overlapping
    an already-uploaded UTC second after a backward step, are never uploaded. No
    correction/import path exists yet.
11. **Data older than 30 days** is marked `expired_for_automatic_upload` and needs an
    owner-enabled import mechanism that does not exist yet on either side.
12. **Interrupted events** are finalized at the last observed second with `incomplete_interval`
    (plus `processing_error` after a crash). The server shows them as finalized; the flag carries
    the truncation.
13. **One channel per collector.** The web app supports several channels per device; this
    collector serves the one named in `[channel] id`.
14. **Clock offset reporting** uses the kernel's adjtimex PLL offset and estimated error. It is
    not an independent measurement of microphone timestamp accuracy (H10).
15. **The fallback timestamp path** (no ADC time) adds a configured fixed latency with
    20 ms of assumed uncertainty. It must be measured on the device.
