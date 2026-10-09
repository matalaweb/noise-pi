# Event detection and evidence

Events are **candidate disturbances**. Nothing is filtered or labeled by source. The owner's
garage door produces events like anything else and is labeled in Laravel.

## Baseline

The baseline is the 20th percentile (numpy linear interpolation) of eligible one-second levels
over the trailing 600 s. It is ready once at least 120 eligible seconds exist. It is kept
separately for `laeq_db`, `low_frequency_leq_db` and `rms_dbfs`. It is a descriptive statistic,
not an L90 and not an Leq.

A second is eligible only if all of these hold: it is complete, unclipped, and not constant; it
qualified no rule; the detector was idle or warming; and it is outside the 30 s recovery after a
post-roll. Candidate, active, and post-roll seconds are never eligible. Because the window
slides, a baseline with no eligible data for 10 minutes becomes *unavailable*. It never adapts
upward to a continuous event.

Baselines reset at each new acquisition session (reconnect, timing epoch) and on profile changes.
Warmup then takes about 2 minutes. Absolute rules still work during warmup.

## Rules (combined with OR)

| Rule | Default |
|---|---|
| A-weighted relative | LAeq >= baseline + 12 dB for 2 consecutive valid seconds |
| Low-frequency relative | baseline + 10 dB for 2 s; **disabled** until `capabilities.low_frequency_validated` |
| Absolute LAeq/LAFmax | disabled until the owner sets values; an LAFmax rule may use `consecutive_seconds: 1` |
| Uncalibrated mode | only `rms_dbfs` relative rules; SPL rules are disabled and reported in the config ack detail |

The event starts at the **first qualifying second** of the confirming run. The rule, its
thresholds, the baseline values, and provenance are frozen into the event.

## Exit, post-roll, retrigger

* Quiet means every evaluable rule is below `threshold - 3 dB` for 5 consecutive complete
  seconds, compared against the frozen event baseline. Null metrics and clipped seconds are
  never quiet.
* The provisional end is the first second of the quiet run. Post-roll is 30 s from that point,
  and the 5 quiet seconds count toward it.
* A rule completing its run before post-roll ends continues the same event and cancels the
  provisional end. After finalization, a new trigger creates a new event.
* Data loss while active ends the event as `incomplete` with `ended_at: null`. Data loss during
  post-roll keeps the known detection end and truncates the recording.
* Threshold changes made during an event apply after it ends. Profile, gain, or deployment
  changes force a split. Recording-disable takes effect immediately; measurements continue.

## Recording

* Recording starts at `first qualifying second - 10 s` (the interval boundary when known).
  Shortfall from startup or a prior gap is preserved and flagged `preroll_shortfall`. Nothing
  is fabricated.
* Recording ends at `provisional end + 30 s`.
* Segments are at most 10 min, and always below 95 MiB, which is under the server's 100 MiB
  limit. At 48 kHz PCM24 a 10 min segment is about 86.4 MB plus a 44 B header, so duration
  limits first.
* Each segment has a contiguous raw sample range with no overlap. Rollover does not reset DSP or
  detection. Segments open lazily, so no empty segments are created.
* Format: mono PCM WAV at the converter's native precision, never padded. The duration is
  authoritative in samples. `duration_ms` is `round(samples * 1000 / fs)`.
* The audio quota is checked before opening each segment. If it is refused, recording stops,
  the event is flagged `audio_coverage_loss`, and measurements continue.
* Event summaries (energy means, max LAFmax) cover detection seconds only.
