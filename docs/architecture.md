# Architecture

## Processes and threads

```
systemd (Type=notify, WatchdogSec)
└── noise-collector run            supervisor: restarts each child independently with backoff,
    │                              feeds the watchdog only while acquisition makes progress
    ├── acquire (process)          one instance per state dir (flock acquisition.lock)
    │   ├── PortAudio callback     copy bytes + timestamps into a preallocated 2 s buffer; nothing else
    │   ├── main/DSP thread        decode, timing, filters, intervals, detector, events, pre-roll ring
    │   └── durability thread      SQLite commits + audio chunk files, strictly in op order
    └── deliver (process)          one instance per state dir (flock delivery.lock)
        ├── control lane           batches, event revisions, config poll/acks, heartbeat, retention
        └── audio lane             declare -> PUT -> complete -> verify, one recording at a time
```

Acquisition and delivery share only SQLite (WAL, short transactions, bounded busy timeouts) and
status files in the runtime directory. A server outage changes `next_attempt_at` values. It
never touches capture. A capture restart never restarts the uploader.

## Component map (spec section 3)

| Spec component | Module |
|---|---|
| AudioSource | `audio/alsa_source.py` (live), `audio/file_source.py` (replay + fault injection) |
| CaptureSupervisor | `acquisition/runner.py` (matching, stream lifecycle, watchdog, reconnect 1/2/5/10/30 s), `audio/discovery.py`, `audio/gain.py` |
| TimeMapper | `timing/mapper.py`, `timing/clock.py` |
| SignalProcessor | `dsp/processor.py`, `dsp/filters.py`, `dsp/calibration.py` |
| EventDetector | `detect/detector.py`, `detect/baseline.py` |
| EvidenceWriter | `evidence/spool.py`, `evidence/wav.py` |
| LocalStore | `store/db.py`, `store/migrations/`, `acquisition/durability.py` |
| ApiClient and Uploader | `transport/api.py`, `delivery/service.py`, `delivery/outbox.py` |
| ConfigManager | `delivery/config_manager.py` (download/validate/stage), `AcquisitionEngine._apply_pending` (apply + ack) |
| HealthReporter | `health/status.py`, `health/logs.py`, heartbeat in `delivery/service.py` |
| CLI | `cli.py`, `doctor.py`, `calibration_check.py` |

## Data flow per block (engine)

1. **Timing**: the block's first-sample monotonic time feeds `TimeMapper`. Any of these is a
   discontinuity: a wall-clock step above 50 ms, three consecutive timestamp residuals beyond
   tolerance, a driver overflow of unknown size, or a sample-index break. A discontinuity ends the
   acquisition session (`boot_id`) and starts a new timing epoch and session.
2. **Pre-roll ring**: decoded analysis-channel integers, sized
   `pre_roll + max confirmation + headroom + max block` (15+ s).
3. **DSP**: float64 copy -> optional correction FIR -> scale -> A, C, LF SOS filters + Fast
   weighting. State persists across blocks and seconds.
4. **Intervals**: boundaries are fixed sample positions computed sequentially. Each second
   starts exactly where the previous one ended, so no sample belongs to two seconds. Incomplete,
   settling, or lossy seconds become local `omitted` rows and are never uploaded.
5. **Detector -> events**: event start/end and recording control. Audio is pumped from the ring
   into the active segment after each block, so pre-roll copies never skip or duplicate samples.
6. **Ops** go to the durability thread through a bounded backlog (op count and audio bytes). If
   the backlog is full, the loss is counted and recordings are cut as incomplete. Nothing is
   claimed as stored before commit.

## Durability model

* A measurement and its sequence/high-water mark commit in one `synchronous=FULL` transaction.
* Batches are immutable serialized request bodies created before the first attempt.
* Audio is written as 5 s chunks (fsync at most every 1 s of active data, then rename and
  directory fsync). Finalization: build `.wav.tmp`, fsync, verify, hash on disk, rename, fsync
  directory, commit, and only then delete the chunks.
* Startup recovery: remove orphan temp files, finalize interrupted segments from complete
  chunk samples as `incomplete`, adopt orphan manifests, and close open sessions. Open events
  get a final revision ended at the last observed second, flagged `incomplete_interval` and
  `processing_error`.
* Limitation: the in-memory pre-roll ring and the unsynced tail (at most about 1 s of chunk data
  plus the DSP/durability backlog) can be lost on sudden power failure. This assumes the storage
  device honors flushes; arbitrary SD cards are not promised to be power-loss safe.
