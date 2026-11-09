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
        notes="Placeholder adapter for M0 orchestration testing. Not AI inference.",
    )

    async def validate(self, request: dict[str, Any]) -> list[str]:
        errors = []
        if not (request.get("prompt") or "").strip():
            errors.append("prompt is required")
        return errors

    async def run(self, handle: JobHandle) -> AsyncIterator[dict[str, Any]]:
        prompt = handle.request.get("prompt", "")
        seed = int(handle.request.get("seed", 0))
        digest = hashlib.sha256(f"{prompt}|{seed}".encode()).digest()
        r, g, b = digest[0] // 2 + 40, digest[1] // 2 + 60, digest[2] // 2 + 80

        steps = 8
        for i in range(steps):
            await asyncio.sleep(0.25)  # simulated work; keeps progress observable
            yield {"progress": (i + 1) / (steps + 1), "message": f"Mock stage {i + 1}/{steps}"}

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

        yield {
            "done": True,
            "progress": 1.0,
            "output": {"path": str(out_path), "sha256": sha, "kind": "image", "size": [512, 512]},
        }
