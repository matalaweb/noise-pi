#!/usr/bin/env python3
"""Measure engine CPU cost and memory (run on the Pi before enabling features).

    python scripts/bench_dsp.py --seconds 300 [--correction] [--block 480]

Reports the real-time factor (audio seconds processed per CPU second) for the full engine
(DSP + intervals + detector + recording handoff) with storage ops discarded, plus peak RSS.
Target on a Pi 4: well under one core for scalar metrics (spec section 19).
"""

from __future__ import annotations

import argparse
import resource
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import numpy as np  # noqa: E402

from noise_collector.acquisition.engine import AcquisitionEngine, CapturedBlock, EngineLocalSettings  # noqa: E402
from noise_collector.audio.pcm import PcmFormat  # noqa: E402
from noise_collector.contract.examples import CALIBRATION_IDS, example_configuration, local_inputs  # noqa: E402
from noise_collector.synth import Burst, Scenario, to_pcm  # noqa: E402
from noise_collector.timing.clock import SimulatedClock  # noqa: E402


class NullSink:
    n = 0

    def submit(self, op):
        self.n += 1
        return True


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seconds", type=float, default=300)
    ap.add_argument("--block", type=int, default=480)
    ap.add_argument("--correction", action="store_true", help="enable a min-phase FIR response correction")
    args = ap.parse_args()
    fs = 48000
    # Render a short SYNTHETIC loop and cycle it, so the measured RSS is the engine's, not the generator's.
    sc = Scenario(duration_s=30, background_dbfs=-55, bursts=[Burst(10, 12, "engine_like", -30)])
    loop = to_pcm(sc.render())
    rss0 = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    if args.correction:
        local = local_inputs("calibrated")
        f = np.geomspace(10, 20000, 40)
        local.calibrations[CALIBRATION_IDS["calibrated"]].response_curve = tuple(
            (float(a), float(-1.5 * np.exp(-((np.log10(a) - 1.3) ** 2) / 0.1))) for a in f)
        cfg = example_configuration(local=local)
    else:
        cfg = example_configuration()
    clock = SimulatedClock(1_790_000_000.0 - 1000.0)
    eng = AcquisitionEngine(config=cfg, local=EngineLocalSettings(), fmt=PcmFormat("int24", 24, 1, fs), microphone={},
                            sink=NullSink(), clock=clock)
    eng.start_stream("bench")
    t_cpu, t_wall = time.process_time(), time.perf_counter()
    total = int(args.seconds * fs)
    for n in range(0, total - args.block, args.block):
        a = n % (len(loop) - args.block)
        eng.on_block(CapturedBlock(loop[a : a + args.block], n, 1000.0 + n / fs, "synthetic", clock.offset))
    cpu = time.process_time() - t_cpu
    wall = time.perf_counter() - t_wall
    rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    unit = (1024 * 1024) if sys.platform == "darwin" else 1024
    rss_mb, rss0_mb = rss / unit, rss0 / unit
    print(f"audio {args.seconds:.0f} s, block {args.block}, correction={args.correction}")
    print(f"cpu {cpu:.2f} s  wall {wall:.2f} s  real-time factor {args.seconds / cpu:.1f}x  "
          f"=> {100 * cpu / args.seconds:.1f}% of one core")
    print(f"peak RSS {rss_mb:.0f} MiB (before engine: {rss0_mb:.0f} MiB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
