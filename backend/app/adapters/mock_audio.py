"""Mock audio adapter — proves the dialogue→audio job path end-to-end.

Produces a real, decodable 16-bit PCM WAV locally (no model, no
network, no cost) by synthesizing deterministic speech-SHAPED tones:
each word becomes an amplitude-enveloped harmonic blend keyed to the
segment's voice seed + word hash, with punctuation pauses. The output
is structured tones — clearly labeled simulated audio, never
presented as real speech. Duration is the deterministic plan the
preview committed to (est_duration_s), so preview/commit/probe agree.

Deterministic cancellation uses the same `hold` barrier contract as
mock.image/mock.video; chunked PCM writing checkpoints the cancel
event between words — no subprocess needed.
"""
from __future__ import annotations

import asyncio
import hashlib
import math
import struct
import time
import wave
from pathlib import Path
from typing import Any, AsyncIterator

from app.adapters.base import CapabilityProfile, JobHandle

DEFAULT_RATE = 48000


def _voice_seed(request: dict[str, Any]) -> bytes:
    """One deterministic voice signature: speaker + resolved voice +
    segment seed → the harmonic palette every word draws from."""
    voice = request.get("voice") or {}
    basis = "|".join(str(v) for v in (
        request.get("character_id"), voice.get("profile"),
        voice.get("profile_override"), voice.get("style"),
        voice.get("delivery"), voice.get("emotion"),
        request.get("language"), request.get("seed", 0)))
    return hashlib.sha256(basis.encode()).digest()


def _word_envelope(word: str, vseed: bytes, n_samples: int,
                   rate: int) -> list[int]:
    """One word → n PCM samples: an attack/decay envelope over two
    harmonics whose pitches come from the word+voice hash. Audibly
    structured tones — recognizably NOT speech."""
    h = hashlib.sha256(vseed + word.encode()).digest()
    f0 = 110 + (h[0] % 180)          # base pitch 110–290 Hz
    f1 = f0 * (1.5 + (h[1] % 100) / 200.0)   # overtone 1.5–2.0×
    vib = 4 + (h[2] % 4)             # 4–7 Hz slow vibrato
    depth = 0.04 + (h[3] % 20) / 500.0
    out = []
    attack = max(1, n_samples // 8)
    for i in range(n_samples):
        t = i / rate
        env = min(1.0, i / attack) * max(0.0, 1.0 - i / n_samples) ** 0.7
        wob = 1.0 + depth * math.sin(2 * math.pi * vib * t)
        s = (0.55 * math.sin(2 * math.pi * f0 * wob * t)
             + 0.30 * math.sin(2 * math.pi * f1 * t)
             + 0.10 * math.sin(2 * math.pi * f0 * 2 * t))
        out.append(int(max(-1.0, min(1.0, s * env)) * 24000))
    return out


class MockAudioAdapter:
    profile = CapabilityProfile(
        adapter_id="mock.audio",
        display_name="Mock Dialogue Generator",
        adapter_kind="mock",
        tasks=("dialogue2audio", "text2audio"),
        supports={"seed": True, "duration": True, "language": True,
                  "voice": True},
        limits={"max_duration_s": 600, "sample_rates": (48000, 44100,
                                                       22050),
                "channels": 1},
        requires_cloud=False,
        cost_model="free",
        lane="gen",
        needs_gpu=False,
        notes="Placeholder adapter for A5 dialogue orchestration "
              "testing. Deterministic shaped tones — not speech "
              "synthesis.",
    )

    async def validate(self, request: dict[str, Any]) -> list[str]:
        errors = []
        if not (request.get("text") or "").strip():
            errors.append("text is required")
        dur = request.get("duration_s")
        if dur is None or not isinstance(dur, (int, float)) \
                or dur <= 0 or dur > 600:
            errors.append("duration_s is required (0, 600]")
        rate = request.get("sample_rate", DEFAULT_RATE)
        if rate not in self.profile.limits["sample_rates"]:
            errors.append("unsupported sample_rate")
        return errors

    async def run(self, handle: JobHandle) -> AsyncIterator[dict[str, Any]]:
        text = (handle.request.get("text") or "").strip()
        direction = handle.request.get("direction") or ""
        seed = int(handle.request.get("seed", 0))
        dur_s = float(handle.request.get("duration_s") or 1.0)
        rate = int(handle.request.get("sample_rate") or DEFAULT_RATE)
        vseed = _voice_seed(handle.request)

        work = Path(handle.work_dir)
        work.mkdir(parents=True, exist_ok=True)
        out_path = work / f"{handle.job_id}.wav"

        hold = handle.request.get("hold") or {}
        hold_at = int(hold.get("at_stage", 0) or 0)

        async def _barrier(stage: int):
            """Park deterministically at `stage` until release/cancel —
            same contract as the image/video mocks."""
            if hold_at != stage:
                return
            release = hold.get("release_file")
            deadline = time.monotonic() + float(
                hold.get("timeout_s", 300))
            while True:
                handle.check_cancel()
                if release and Path(release).exists():
                    return
                if time.monotonic() > deadline:
                    raise RuntimeError("mock hold barrier timed out")
                yield {"progress": 0.4,
                       "message": "Mock audio hold — awaiting "
                                  "release/cancel"}
                await asyncio.sleep(0.05)

        # stage 1 — plan the word layout across the committed duration
        words = [w for w in text.split() if w.strip()] or ["…"]
        total = int(dur_s * rate)
        gap = int(0.05 * rate)               # 50 ms inter-word gaps
        body = max(rate // 4, total - gap * (len(words) - 1))
        per = max(1, body // len(words))
        yield {"progress": 0.1,
               "message": f"Mock synth plan — {len(words)} words"}
        async for ev in _barrier(1):
            yield ev

        # stage 2 — synthesize word chunks straight into the WAV;
        # cancel checkpoints between words keep cancellation exact.
        written = 0
        wf = wave.open(str(out_path), "wb")
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(rate)
        try:
            for i, w in enumerate(words):
                handle.check_cancel()
                n = per if i < len(words) - 1 else max(
                    1, total - written - gap * (len(words) - 1 - i))
                frames = _word_envelope(w, vseed, n, rate)
                wf.writeframesraw(
                    struct.pack(f"<{len(frames)}h", *frames))
                written += n
                if i < len(words) - 1:
                    wf.writeframesraw(b"\x00\x00" * gap)
                    written += gap
                yield {"progress": 0.15 + 0.6 * (i + 1) / len(words),
                       "message": f"Mock synth word {i + 1}/{len(words)}"}
                async for ev in _barrier(2):
                    yield ev
                await asyncio.sleep(0)
            # pad/trim to the committed duration exactly
            if written < total:
                wf.writeframesraw(b"\x00\x00" * (total - written))
        finally:
            wf.close()

        # stage 3 — post-synth barrier before publish
        async for ev in _barrier(3):
            yield ev
        yield {"progress": 0.85, "message": "Mock probe"}
        # Honest metadata: ffprobe the real WAV through the app's own
        # media engine — nothing fabricated. decode_check additionally
        # guarantees the payload actually decodes end-to-end (not just
        # a parseable header — truncated/corrupt output fails here).
        from app.domain.media import engine as media_engine
        probe = media_engine.probe_audio(
            out_path, cancel=handle.cancel_event)
        if not probe.get("ok"):
            raise RuntimeError(
                f"mock output failed probe: {probe.get('reason')}")
        dec = media_engine.decode_check_audio(
            out_path, cancel=handle.cancel_event)
        if not dec.get("ok"):
            raise RuntimeError(
                f"mock output failed decode: {dec.get('reason')}")
        handle.check_cancel()
        sha = hashlib.sha256(out_path.read_bytes()).hexdigest()
        text_sha = hashlib.sha256(text.encode()).hexdigest()
        out: dict[str, Any] = {
            "done": True,
            "progress": 1.0,
            "output": {
                "path": str(out_path), "sha256": sha, "kind": "audio",
                "name": f"mock-dialogue-{handle.job_id[:8]}.wav",
                "origin": "generation",
                "media": {
                    "source": "mock", "generated": False,
                    "is_mock": True,
                    "adapter_id": self.profile.adapter_id,
                    "seed": seed,
                    "text_sha256": text_sha,
                    "direction": direction,
                    "language": handle.request.get("language") or "en",
                    "simulated_speech": True,   # tones, not synthesis
                    "duration_s": dur_s,
                    "sample_rate": rate,
                    "probe": probe,
                },
            },
        }
        if handle.request.get("segment_id"):
            # A5 — land the produced asset on the dialogue segment as a
            # normal audited, undoable command, with the same stale
            # pins as keyframe/clip results.
            out["output"]["apply_command"] = {
                "command_type": "dialogue.result",
                "payload": {
                    "segment_id": handle.request["segment_id"],
                    "result_asset_id": "__asset__",
                    "job_id": "__job__",
                    "expected_segment_revision":
                        handle.request.get("expected_result_revision"),
                    "expected_sources":
                        handle.request.get("expected_sources")},
            }
        yield out
