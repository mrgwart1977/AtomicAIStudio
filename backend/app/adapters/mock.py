"""Mock image adapter — proves the job/orchestration path end-to-end.

Produces a real PNG file locally (no model, no network, no cost) so the
full prompt -> preview -> commit -> job -> asset pipeline is testable
before any inference adapter exists. Clearly labeled as a mock.
"""

from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path
from typing import Any, AsyncIterator

from PIL import Image, ImageDraw

from app.adapters.base import CapabilityProfile, JobHandle


class MockImageAdapter:
    profile = CapabilityProfile(
        adapter_id="mock.image",
        display_name="Mock Image Generator",
        adapter_kind="mock",
        tasks=("text2image",),
        supports={"seed": True, "size": True, "negative_prompt": False},
        limits={"width": 512, "height": 512},
        requires_cloud=False,
        cost_model="free",
        lane="gen",
        needs_gpu=False,
        notes="Placeholder adapter for M0 orchestration testing. Not AI inference.",
    )

    async def validate(self, request: dict[str, Any]) -> list[str]:
        errors = []
        if not (request.get("prompt") or "").strip():
            errors.append("prompt is required")
        return errors

    async def run(self, handle: JobHandle) -> AsyncIterator[dict[str, Any]]:
        import time

        prompt = handle.request.get("prompt", "")
        seed = int(handle.request.get("seed", 0))
        digest = hashlib.sha256(f"{prompt}|{seed}".encode()).digest()
        r, g, b = digest[0] // 2 + 40, digest[1] // 2 + 60, digest[2] // 2 + 80

        steps = 8
        hold = handle.request.get("hold") or {}
        hold_at = int(hold.get("at_stage", 0) or 0)
        for i in range(steps):
            await asyncio.sleep(0.25)  # simulated work; keeps progress observable
            yield {"progress": (i + 1) / (steps + 1), "message": f"Mock stage {i + 1}/{steps}"}
            if hold_at and i + 1 == hold_at:
                # Deterministic test barrier (mock only): the job stays
                # running until a release_file appears OR the engine's
                # cancel event fires — never a timing guess. A bounded
                # failsafe prevents a forgotten barrier hanging a lane.
                release = hold.get("release_file")
                deadline = time.monotonic() + float(hold.get("timeout_s", 300))
                while True:
                    handle.check_cancel()
                    if release and Path(release).exists():
                        break
                    if time.monotonic() > deadline:
                        raise RuntimeError("mock hold barrier timed out")
                    yield {"progress": (i + 1) / (steps + 1),
                           "message": "Mock hold — awaiting release/cancel"}
                    await asyncio.sleep(0.05)
                handle.check_cancel()

        img = Image.new("RGB", (512, 512), (16, 24, 36))
        draw = ImageDraw.Draw(img)
        for y in range(512):  # simple deterministic gradient keyed to the prompt
            t = y / 512
            draw.line([(0, y), (512, y)], fill=(int(r * t), int(g * t), int(b * t)))
        draw.rectangle([24, 24, 488, 488], outline=(114, 221, 205), width=3)
        draw.text((40, 40), "MOCK OUTPUT — no AI inference", fill=(174, 187, 208))
        draw.text((40, 70), prompt[:60] or "(empty prompt)", fill=(237, 243, 255))

        out_dir = Path(handle.work_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / f"{handle.job_id}.png"
        img.save(out_path, format="PNG")
        sha = hashlib.sha256(out_path.read_bytes()).hexdigest()

        out: dict[str, Any] = {
            "done": True,
            "progress": 1.0,
            "output": {
                "path": str(out_path), "sha256": sha, "kind": "image", "size": [512, 512],
                "name": f"mock-{handle.job_id[:8]}.png", "origin": "generation",
                "media": {
                    "source": "mock", "generated": False, "is_mock": True,
                    "adapter_id": self.profile.adapter_id,
                    "prompt": prompt, "seed": seed,
                },
            },
        }
        if handle.request.get("keyframe_id"):
            # A3 — land the produced asset on the keyframe as a normal
            # audited, undoable command (never a silent write). The pins
            # let keyframe.result refuse a stale/superseded job: only the
            # keyframe's CURRENT generation on an UNCHANGED keyframe may
            # attach a result.
            out["output"]["apply_command"] = {
                "command_type": "keyframe.result",
                "payload": {
                    "keyframe_id": handle.request["keyframe_id"],
                    "shot_id": handle.request.get("shot_id"),
                    "result_asset_id": "__asset__",
                    "job_id": "__job__",
                    "expected_keyframe_revision":
                        handle.request.get("expected_result_revision"),
                    "expected_planning_sha":
                        handle.request.get("expected_planning_sha")},
            }
        elif handle.request.get("shot_id") and handle.request.get("set_thumbnail"):
            out["output"]["apply_command"] = {
                "command_type": "shot.update",
                "payload": {"shot_id": handle.request["shot_id"],
                            "thumbnail_asset_id": "__asset__"},
            }
        yield out
