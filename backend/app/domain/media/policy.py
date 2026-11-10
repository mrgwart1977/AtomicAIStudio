"""Validation policy — what a generated/imported video must satisfy.

Tolerances are documented and deliberate:
- duration: containers (esp. WebM) round timestamps to time-base units;
  allow max(duration_slop_s, one frame at expected fps).
- fps: compared with fps_rel_tol relative tolerance (fractional fps like
  23.976 vs 24 are different encodings, not rounding — keep tolerance tight).
- frames: when the generation contract specifies an exact frame count we
  compare the DECODED frame count (ffprobe -count_frames / nb_read_frames),
  never container metadata or fps*duration — VFR sources lie.
"""
from __future__ import annotations

from dataclasses import dataclass, field

DEFAULT_CODECS = frozenset({
    "vp8", "vp9", "av1", "h264", "hevc", "mpeg4", "mpeg2video", "theora",
    "prores", "mjpeg", "wmv3", "msmpeg4v3",
})


@dataclass
class ValidationSpec:
    width: int | None = None            # expected pixels
    height: int | None = None
    fps: float | None = None            # expected nominal fps
    frames: int | None = None           # exact decoded-frame contract (gen jobs)
    duration_s: float | None = None     # expected duration (soft check)
    codecs: frozenset[str] = DEFAULT_CODECS
    duration_slop_s: float = 0.5        # container/time-base rounding
    fps_rel_tol: float = 0.01           # 1%
    probe_timeout_s: float = 30.0
    decode_timeout_s: float = 120.0
    require_full_decode: bool = True    # production: always True
    extra: dict = field(default_factory=dict)

    def frame_slop_s(self) -> float:
        return 1.0 / self.fps if self.fps and self.fps > 0 else 0.0

    def duration_tolerance_s(self) -> float:
        return max(self.duration_slop_s, self.frame_slop_s())
