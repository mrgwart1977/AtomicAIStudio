"""Mock video adapter — proves the keyframe→video job path end-to-end.

Produces a real, decodable H.264 MP4 locally (no model, no network, no
cost) by rendering deterministic PIL frames and encoding them with the
app's own bundled ffmpeg — the same binary the timeline exporter uses.
Clearly labeled as a mock; output media marks is_mock/generated=False.

Deterministic cancellation uses the same `hold` barrier contract as
mock.image; the ffmpeg subprocess itself is also cancellable via the
engine's cancel event (supervise.run).
"""
from __future__ import annotations

import asyncio
import hashlib
import time
from pathlib import Path
from typing import Any, AsyncIterator

from PIL import Image, ImageDraw

from app.adapters.base import CapabilityProfile, JobHandle


def _resolve_ffmpeg() -> str:
    from app.domain.media import binaries
    hit = binaries.find_binary("ffmpeg")
    if hit is None:
        raise RuntimeError("mock.video needs the bundled ffmpeg binary")
    return hit["path"]


def _h264(ffmpeg: str) -> str:
    from app.domain.media import supervise
    from app.domain.timeline_export import h264_encoder
    return h264_encoder(
        ffmpeg, lambda argv, timeout_s: supervise.run(argv, timeout_s=timeout_s))


class MockVideoAdapter:
    profile = CapabilityProfile(
        adapter_id="mock.video",
        display_name="Mock Video Generator",
        adapter_kind="mock",
        tasks=("keyframe2video", "text2video"),
        supports={"seed": True, "size": True, "fps": True,
                  "duration": True, "negative_prompt": False,
                  "start_frame": True, "end_frame": True},
        limits={"width": 4096, "height": 4096, "fps": 480,
                "max_frames": 480 * 60},
        requires_cloud=False,
        cost_model="free",
        lane="gen",
        needs_gpu=False,
        notes="Placeholder adapter for A4 motion-clip orchestration "
              "testing. Not AI inference — deterministic frames only.",
    )

    async def validate(self, request: dict[str, Any]) -> list[str]:
        errors = []
        if not (request.get("prompt") or "").strip():
            errors.append("prompt is required")
        frames = request.get("duration_frames")
        if frames is None or not isinstance(frames, int) or frames < 1:
            errors.append("duration_frames is required (≥1)")
        return errors

    async def run(self, handle: JobHandle) -> AsyncIterator[dict[str, Any]]:
        prompt = handle.request.get("prompt", "")
        seed = int(handle.request.get("seed", 0))
        frames = int(handle.request.get("duration_frames") or 24)
        fps_num = int((handle.request.get("fps") or {}).get("num") or 24)
        fps_den = int((handle.request.get("fps") or {}).get("den") or 1)
        w = int(handle.request.get("width") or 512)
        h = int(handle.request.get("height") or 512)
        # h264 needs even dims
        w -= w % 2
        h -= h % 2
        digest = hashlib.sha256(f"{prompt}|{seed}".encode()).digest()
        r0, g0, b0 = digest[0] // 2 + 40, digest[1] // 2 + 60, digest[2] // 2 + 80

        work = Path(handle.work_dir)
        work.mkdir(parents=True, exist_ok=True)
        frames_dir = work / "frames"
        frames_dir.mkdir(exist_ok=True)

        hold = handle.request.get("hold") or {}
        hold_at = int(hold.get("at_stage", 0) or 0)
        stages = 6  # render(3 stages) + encode + probe + done
        progress = 0.0

        def _prog(msg: str, p: float) -> dict[str, Any]:
            return {"progress": p, "message": msg}

        async def _barrier(stage: int):
            """Park deterministically at `stage` until release/cancel —
            same contract as mock.image's hold, with the observable
            'Mock video hold' message the job list exposes."""
            if hold_at != stage:
                return
            release = hold.get("release_file")
            deadline = time.monotonic() + float(hold.get("timeout_s", 300))
            while True:
                handle.check_cancel()
                if release and Path(release).exists():
                    return
                if time.monotonic() > deadline:
                    raise RuntimeError("mock hold barrier timed out")
                yield {"progress": 0.55,
                       "message": "Mock video hold — awaiting release/cancel"}
                await asyncio.sleep(0.05)

        # stages 1–3: render deterministic frames (a moving box keyed
        # to prompt+seed — predictable position per frame index)
        for i in range(frames):
            if i % max(1, frames // 3) == 0:
                handle.check_cancel()
                progress = (i / frames) * 0.5
                yield _prog(f"Mock render frame {i}/{frames}", progress)
                async for ev in _barrier(
                        1 if progress < 0.2 else
                        (2 if progress < 0.4 else 3)):
                    yield ev
                await asyncio.sleep(0)  # keep the loop cancellable
            img = Image.new("RGB", (w, h), (16, 24, 36))
            d = ImageDraw.Draw(img)
            t = i / max(1, frames - 1)
            # deterministic motion: box travels left→right, tinted by seed
            x = int(24 + (w - 120) * t)
            d.rectangle([x, h // 2 - 40, x + 96, h // 2 + 40],
                        fill=(int(r0 * (0.4 + 0.6 * t)),
                              int(g0 * (0.4 + 0.6 * t)),
                              int(b0 * (0.4 + 0.6 * t))))
            d.rectangle([8, 8, w - 8, h - 8], outline=(114, 221, 205), width=2)
            d.text((16, 16), f"MOCK VIDEO f{i} — no AI inference",
                   fill=(174, 187, 208))
            d.text((16, 34), prompt[:48] or "(empty prompt)",
                   fill=(237, 243, 255))
            img.save(frames_dir / f"f{i:05d}.png", format="PNG")

        ffmpeg = _resolve_ffmpeg()
        encoder = _h264(ffmpeg)
        out_path = work / f"{handle.job_id}.mp4"
        argv = [ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
                "-framerate", f"{fps_num}/{fps_den}",
                "-i", str(frames_dir / "f%05d.png"),
                "-c:v", encoder, "-pix_fmt", "yuv420p",
                "-movflags", "+faststart", str(out_path)]
        yield _prog("Mock encode", 0.6)
        async for ev in _barrier(4):
            yield ev
        from app.domain.media import supervise
        res = supervise.run(argv, timeout_s=300.0,
                            cancel=handle.cancel_event)
        handle.check_cancel()  # cancelled mid-encode → JobCancelled
        if res.get("timed_out"):
            raise RuntimeError("mock encode timed out")
        if res.get("exit") != 0:
            tail = (res.get("output_tail") or "").strip()[-300:]
            raise RuntimeError(f"mock encode failed: {tail}")

        yield _prog("Mock probe", 0.85)
        async for ev in _barrier(5):
            yield ev
        # Honest metadata: ffprobe the real encoded file through the
        # app's own media engine — nothing fabricated.
        from app.domain.media import engine as media_engine
        probe = media_engine.probe(out_path, cancel=handle.cancel_event)
        if not probe.get("ok"):
            raise RuntimeError(
                f"mock output failed probe: {probe.get('reason')}")
        handle.check_cancel()
        sha = hashlib.sha256(out_path.read_bytes()).hexdigest()
        duration_s = round(frames * fps_den / fps_num, 3)
        out: dict[str, Any] = {
            "done": True,
            "progress": 1.0,
            "output": {
                "path": str(out_path), "sha256": sha, "kind": "video",
                "size": [w, h],
                "name": f"mock-clip-{handle.job_id[:8]}.mp4",
                "origin": "generation",
                "media": {
                    "source": "mock", "generated": False, "is_mock": True,
                    "adapter_id": self.profile.adapter_id,
                    "prompt": prompt, "seed": seed,
                    "fps_num": fps_num, "fps_den": fps_den,
                    "duration_s": duration_s, "frames": frames,
                    # Real ffprobe of the real file — the timeline's
                    # fps/duration math reads media.probe verbatim.
                    "probe": probe,
                },
            },
        }
        if handle.request.get("clip_id"):
            # A4 — land the produced asset on the motion clip as a
            # normal audited, undoable command, with the same stale
            # pins as keyframe results.
            out["output"]["apply_command"] = {
                "command_type": "clip.result",
                "payload": {
                    "clip_id": handle.request["clip_id"],
                    "shot_id": handle.request.get("shot_id"),
                    "result_asset_id": "__asset__",
                    "job_id": "__job__",
                    "expected_clip_revision":
                        handle.request.get("expected_result_revision"),
                    "expected_planning_sha":
                        handle.request.get("expected_planning_sha"),
                    "expected_sources":
                        handle.request.get("expected_sources")},
            }
        yield out
