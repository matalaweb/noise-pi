"""Continuous calibrated signal processing and one-second interval metrics.

Metric definitions (interval = start-inclusive/end-exclusive UTC second, actual covered samples):

    laeq_db              10 log10(mean(pA^2) / p0^2)
    lceq_db              10 log10(mean(pC^2) / p0^2)
    lafmax_db            max over samples of 10 log10(q / p0^2), q = Fast-weighted pA^2 (tau 125 ms)
    lcpeak_db            null: transient/bandwidth validation not done (sample peak kept locally)
    low_frequency_leq_db 10 log10(mean(pLF^2) / p0^2), pLF = Butterworth 20-125 Hz output
    rms_dbfs             20 log10(rms(x)), x normalised so full-scale peak = 1 (DC included)

Without a valid absolute scale the SPL fields are null; the same weighted quantities are kept
locally in dB re full scale (``*_dbfs`` diagnostics) and never written into SPL fields.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np

from ..audio.pcm import PcmFormat
from .calibration import P0
from .filters import FastWeighting, SosFilter, StreamingFir, lf_filter, load_filter

DEFAULT_SETTLE_SECONDS = 2.0


@dataclass
class ProcessedBlock:
    """Per-sample processor outputs for one block, aligned to the raw input."""

    raw: np.ndarray  # int32 valid-bit samples
    x: np.ndarray  # normalised
    pa2: np.ndarray  # A-weighted squared (Pa^2 or FS^2 when unscaled)
    pc2: np.ndarray
    pc_abs: np.ndarray
    plf2: np.ndarray
    q: np.ndarray  # Fast-weighted pA^2

    def __len__(self) -> int:
        return len(self.raw)

    def slice(self, a: int, b: int) -> "ProcessedBlock":
        return ProcessedBlock(*(arr[a:b] for arr in (self.raw, self.x, self.pa2, self.pc2, self.pc_abs, self.plf2, self.q)))


class SignalProcessor:
    def __init__(
        self,
        fmt: PcmFormat,
        scale_pa_per_fs: float | None,
        correction_taps: np.ndarray | None = None,
        settle_seconds: float = DEFAULT_SETTLE_SECONDS,
        lf_band_hz: tuple[float, float] = (20.0, 125.0),
    ) -> None:
        fs = fmt.sample_rate
        self.fmt = fmt
        self.scale = scale_pa_per_fs
        self.filter_a = SosFilter(load_filter("A", fs).sos)
        self.filter_c = SosFilter(load_filter("C", fs).sos)
        lf = lf_filter(fs, lf_band_hz)
        self.filter_lf = SosFilter(lf.sos)
        self.fast = FastWeighting(fs)
        self.correction = StreamingFir(correction_taps) if correction_taps is not None else None
        self.settle_samples = int(round(settle_seconds * fs))
        self.settled_from_sample = 0
        self.filter_hashes = {"A": load_filter("A", fs).sha256, "C": load_filter("C", fs).sha256, "LF": lf.sha256}

    def reset(self, at_sample: int) -> None:
        """Reset all filter state after a real discontinuity or profile change."""
        for f in (self.filter_a, self.filter_c, self.filter_lf, self.fast):
            f.reset()
        if self.correction is not None:
            self.correction.reset()
        self.settled_from_sample = at_sample + self.settle_samples

    def process(self, raw: np.ndarray) -> ProcessedBlock:
        x = raw.astype(np.float64) / float(self.fmt.full_scale)
        cx = self.correction(x) if self.correction is not None else x
        p = cx * self.scale if self.scale is not None else cx
        pa = self.filter_a(p)
        pc = self.filter_c(p)
        plf = self.filter_lf(p)
        pa2 = pa * pa
        return ProcessedBlock(raw=raw, x=x, pa2=pa2, pc2=pc * pc, pc_abs=np.abs(pc), plf2=plf * plf, q=self.fast(pa2))


@dataclass
class QualitySettings:
    near_full_scale_dbfs: float = -1.0
    dc_offset_dbfs: float = -40.0


@dataclass
class IntervalAccumulator:
    utc_second: int
    first_sample: int
    n: int = 0
    sum_x: float = 0.0
    sum_x2: float = 0.0
    sum_pa2: float = 0.0
    sum_pc2: float = 0.0
    sum_plf2: float = 0.0
    max_q: float = 0.0
    max_abs_pc: float = 0.0
    clip_pos: int = 0
    clip_neg: int = 0
    near_fs: int = 0
    raw_min: int | None = None
    raw_max: int | None = None
    unsettled: bool = False
    invalid_reasons: set[str] = field(default_factory=set)
    flags: set[str] = field(default_factory=set)

    def add(self, blk: ProcessedBlock, fmt: PcmFormat, near_fs_threshold: int, settled_from: int) -> None:
        if len(blk) == 0:
            return
        if self.first_sample + self.n < settled_from:
            self.unsettled = True
        self.n += len(blk)
        self.sum_x += float(blk.x.sum())
        self.sum_x2 += float(np.dot(blk.x, blk.x))
        self.sum_pa2 += float(blk.pa2.sum())
        self.sum_pc2 += float(blk.pc2.sum())
        self.sum_plf2 += float(blk.plf2.sum())
        self.max_q = max(self.max_q, float(blk.q.max()))
        self.max_abs_pc = max(self.max_abs_pc, float(blk.pc_abs.max()))
        r = blk.raw
        self.clip_pos += int(np.count_nonzero(r >= fmt.rail_positive))
        self.clip_neg += int(np.count_nonzero(r <= fmt.rail_negative))
        self.near_fs += int(np.count_nonzero(np.abs(r.astype(np.int64)) >= near_fs_threshold))
        lo, hi = int(r.min()), int(r.max())
        self.raw_min = lo if self.raw_min is None else min(self.raw_min, lo)
        self.raw_max = hi if self.raw_max is None else max(self.raw_max, hi)


def _db_power(mean_square: float, ref_sq: float) -> float | None:
    if not math.isfinite(mean_square) or mean_square <= 0.0:
        return None
    return 10.0 * math.log10(mean_square / ref_sq)


@dataclass
class IntervalResult:
    utc_second: int
    first_sample: int
    sample_count: int
    complete: bool
    omit_reason: str | None
    metrics: dict[str, float | None]
    null_reasons: dict[str, str]
    quality_flags: list[str]
    diagnostics: dict

    @property
    def all_null(self) -> bool:
        return all(v is None for v in self.metrics.values())

    @property
    def clipped(self) -> bool:
        return "clipped" in self.quality_flags

    @property
    def baseline_eligible(self) -> bool:
        return self.complete and not self.clipped and "suspect_constant_input" not in self.quality_flags


def finalize_interval(
    acc: IntervalAccumulator,
    fmt: PcmFormat,
    *,
    scaled: bool,
    spl_allowed: bool,
    noise_floor_laeq_db: float | None,
    quality: QualitySettings,
    timestamp_fallback: bool,
) -> IntervalResult:
    """Convert accumulated energies to metrics, null handling and quality flags."""
    null_reasons: dict[str, str] = {}
    flags: set[str] = set(acc.flags)
    diag: dict = {
        "sample_count": acc.n,
        "clip_positive": acc.clip_pos,
        "clip_negative": acc.clip_neg,
        "near_full_scale_samples": acc.near_fs,
    }
    complete = not acc.invalid_reasons and not acc.unsettled and acc.n > 0
    omit_reason = None
    if acc.n == 0:
        omit_reason = "no_samples"
    elif acc.invalid_reasons:
        omit_reason = ",".join(sorted(acc.invalid_reasons))
    elif acc.unsettled:
        omit_reason = "filter_settling"

    n = max(acc.n, 1)
    ref = P0 * P0 if scaled else 1.0
    weighted = {
        "laeq": _db_power(acc.sum_pa2 / n, ref),
        "lceq": _db_power(acc.sum_pc2 / n, ref),
        "lafmax": _db_power(acc.max_q, ref),
        "lf": _db_power(acc.sum_plf2 / n, ref),
        "c_sample_peak": _db_power(acc.max_abs_pc**2, ref),
    }
    rms_dbfs = _db_power(acc.sum_x2 / n, 1.0)
    dc = acc.sum_x / n
    diag["dc_offset_fs"] = dc
    if dc != 0 and 20 * math.log10(abs(dc)) > quality.dc_offset_dbfs:
        flags.add("dc_offset")

    constant = acc.raw_min is not None and acc.raw_min == acc.raw_max
    if constant:
        flags.add("suspect_constant_input")
        diag["constant_value"] = acc.raw_min
    if acc.clip_pos or acc.clip_neg:
        flags.add("clipped")
    if acc.near_fs:
        flags.add("near_full_scale")
    if timestamp_fallback:
        flags.add("timestamp_fallback")

    metrics: dict[str, float | None] = {
        "laeq_db": None,
        "lceq_db": None,
        "lafmax_db": None,
        "lcpeak_db": None,
        "low_frequency_leq_db": None,
        "rms_dbfs": rms_dbfs,
    }
    null_reasons["lcpeak_db"] = "capability_disabled"
    if rms_dbfs is None:
        null_reasons["rms_dbfs"] = "zero_energy"

    unit = "db_spl" if scaled else "dbfs"
    diag["weighted_unit"] = unit
    diag.update({f"{k}_{unit}": v for k, v in weighted.items()})

    spl_keys = {"laeq": "laeq_db", "lceq": "lceq_db", "lafmax": "lafmax_db", "lf": "low_frequency_leq_db"}
    if constant:
        for key in spl_keys.values():
            null_reasons[key] = "suspect_constant_input"
    elif not scaled:
        for key in spl_keys.values():
            null_reasons[key] = "uncalibrated"
    elif not spl_allowed:
        flags.add("spl_withheld_gain_mismatch")
        for key in spl_keys.values():
            null_reasons[key] = "gain_mismatch"
    else:
        for src, key in spl_keys.items():
            metrics[key] = weighted[src]
            if weighted[src] is None:
                null_reasons[key] = "zero_energy"
        if noise_floor_laeq_db is not None and metrics["laeq_db"] is not None and metrics["laeq_db"] < noise_floor_laeq_db:
            flags.add("below_noise_floor")

    if constant and rms_dbfs is None:
        omit_reason = omit_reason or "zero_input"
    if complete and all(v is None for v in metrics.values()):
        complete = False
        omit_reason = "all_metrics_null"
    return IntervalResult(
        utc_second=acc.utc_second,
        first_sample=acc.first_sample,
        sample_count=acc.n,
        complete=complete and omit_reason is None,
        omit_reason=omit_reason,
        metrics=metrics,
        null_reasons=null_reasons,
        quality_flags=sorted(flags),
        diagnostics=diag,
    )
