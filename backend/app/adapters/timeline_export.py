"""C5-B — local timeline export adapter (durable job, FFmpeg).

Consumes the pinned `ade-edl/1` document embedded in the job request
by `timeline.export` commit. Two supervised-ffmpeg paths chosen by
`domain.timeline_export.choose_path`:

- `copy`  — concat-demuxer + `-c copy`; only when every clip's
  pinned source shares one stream spec AND is untrimmed.
- `encode` — one-pass filter_complex: per-segment trim → fit-scale
  → pad → setsar → fps, filter-concat at timeline timebase; lavfi
  silence-fill for clips without audio.

All paths resolve from pinned SHA-256 → AssetFile → CAS location —
never from request-supplied paths. Output registers as a CAS video
asset through the job engine's `_register_asset`.
"""
from __future__ import annotations

import asyncio
import shutil
from fractions import Fraction
from pathlib import Path
from typing import Any, AsyncIterator

from app.adapters.base import CapabilityProfile, JobCancelled, JobHandle
from app.db.models import AssetFile
from app.domain.media import binaries, engine, supervise
from app.domain import timeline_export as tx


class TimelineExportAdapter:
    profile = CapabilityProfile(
        adapter_id="ops.timeline.export",
        display_name="Export timeline",
        adapter_kind="local",
        tasks=("export",),
        supports={"profiles": tuple(tx.PROFILES),
                  "crf": (tx.CRF_MIN, tx.CRF_MAX)},
        limits={},
        requires_cloud=False,
        cost_model="free",
        notes="Local FFmpeg render of a pinned ade-edl/1 document. "
              "Stream-copy only for byte-compatible untrimmed sources; "
              "otherwise decode→normalize→encode. Not an AI adapter.",
    )

    def __init__(self, session_factory, storage, settings):
        self._sf = session_factory
        self._storage = storage
        self._settings = settings

    async def validate(self, request: dict[str, Any]) -> list[str]:
        edl = request.get("edl") or {}
        errs: list[str] = []
        if request.get("profile") not in tx.PROFILES:
            errs.append("profile must be one of "
                        + ", ".join(sorted(tx.PROFILES)))
        crf = request.get("crf", tx.CRF_DEFAULT)
        if not isinstance(crf, int) or not (tx.CRF_MIN <= crf <= tx.CRF_MAX):
            errs.append(f"crf must be an integer {tx.CRF_MIN}..{tx.CRF_MAX}")
        if not edl.get("clips"):
            errs.append("timeline has no clips to export")
        return errs

    def _source_path(self, session, sha: str, clip_id: str) -> Path:
        af = session.get(AssetFile, sha)
        if af is None:
            raise ValueError(f"source bytes for clip {clip_id} are missing")
        p = self._storage.file_path(session, af)
        if not p.is_file():
            raise ValueError(
                f"source file for clip {clip_id} is unavailable ({p.name})")
        return p

    @staticmethod
    def _verify_bytes(p: Path, sha: str, clip_id: str) -> None:
        """The pinned sha must match the actual bytes at execution
        time, not just the DB row — a mismatch means the blob on disk
        was corrupted or replaced. Fail closed, never render other
        bytes. Blocking I/O — callers run this via to_thread."""
        from app.domain.storage import hash_file
        actual, _ = hash_file(p)
        if actual != sha:
            raise ValueError(
                f"source bytes for clip {clip_id} do not match the "
                "pinned sha256 — refusing to render")

    def _seg_times(self, clip: dict) -> tuple[float, float]:
        src = clip.get("source") or {}
        fps = Fraction(int(src["fps_num"]), int(src["fps_den"]))
        in_s = float(Fraction(int(clip["in_frame"])) / fps)
        dur_s = float(Fraction(int(clip["out_frame"]) - int(clip["in_frame"]))
                      / fps)
        return in_s, dur_s

    async def run(self, handle: JobHandle) -> AsyncIterator[dict[str, Any]]:
        req = handle.request
        edl: dict[str, Any] = req["edl"]
        clips = edl["clips"]
        profile_name = req["profile"]
        crf = int(req.get("crf") or tx.CRF_DEFAULT)
        work = Path(handle.work_dir)
        work.mkdir(parents=True, exist_ok=True)
        out_path = work / f"{handle.job_id}-export.mp4"
        yield {"progress": 0.05, "stage": "resolving sources"}

        with self._sf() as s:
            paths = [self._source_path(s, c["sha256"], c["clip_id"])
                     for c in clips]
        # Verify pinned bytes + probe off the event loop — hashing and
        # ffprobe are blocking work that would stall the whole backend
        # (and every cancel request) for the duration of a render.
        for p, c in zip(paths, clips):
            await asyncio.to_thread(
                self._verify_bytes, p, c["sha256"], c["clip_id"])
        handle.check_cancel()

        yield {"progress": 0.12, "stage": "probing sources"}
        probes = []
        for p in paths:
            meta = await asyncio.to_thread(
                engine.probe, p, cancel=handle.cancel_event)
            if not meta.get("ok"):
                raise ValueError(
                    f"probe failed for {p.name}: {meta.get('reason')}")
            probes.append(meta)
        handle.check_cancel()

        path_kind, reason = tx.choose_path(edl, probes)
        if path_kind == "copy-not-exact":
            path_kind = "encode"
        yield {"progress": 0.2, "stage": f"{path_kind} path",
               "detail": {"render_path": path_kind,
                          "render_reason": reason}}

        est = int(req.get("est_output_bytes") or
                  tx.est_output_bytes(edl, profile_name))
        free = shutil.disk_usage(work).free
        if free < est:
            raise ValueError(
                f"insufficient disk: need ~{est // 1024 // 1024} MiB free, "
                f"have {free // 1024 // 1024} MiB")

        timeout_s = max(120.0, float(edl.get("total_duration_s") or 0) * 8.0)
        ffmpeg = binaries.ffmpeg()
        if ffmpeg is None:
            raise ValueError("ffmpeg is not resolvable")

        yield {"progress": 0.3, "stage": "rendering"}
        if path_kind == "copy":
            list_path = work / f"{handle.job_id}-concat.txt"
            list_path.write_text(tx.concat_list([str(p) for p in paths]),
                                 encoding="utf-8")
            argv = tx.build_copy_argv(ffmpeg["path"], str(list_path),
                                      str(out_path))
        else:
            target = tx.resolve_profile(edl, profile_name)
            enc = await asyncio.to_thread(
                tx.h264_encoder, ffmpeg["path"], supervise.run)
            segments = []
            for clip, meta, p in zip(clips, probes, paths):
                in_s, dur_s = self._seg_times(clip)
                segments.append({"path": str(p), "in_s": in_s,
                                 "dur_s": dur_s,
                                 "has_audio": bool(meta.get("audio"))})
            argv = tx.build_encode_argv(ffmpeg["path"], segments, target,
                                        crf, str(out_path), encoder=enc)

        r = await asyncio.to_thread(
            supervise.run, argv, timeout_s=timeout_s,
            cancel=handle.cancel_event)
        if r["cancelled"]:
            out_path.unlink(missing_ok=True)
            raise JobCancelled()
        if r["timed_out"]:
            out_path.unlink(missing_ok=True)
            raise ValueError("export timed out")
        if r["exit"] != 0:
            out_path.unlink(missing_ok=True)
            tail = (r["output_tail"] or "ffmpeg failed").strip()[-300:]
            raise ValueError(f"ffmpeg export failed: {tail}")
        if not out_path.is_file() or out_path.stat().st_size == 0:
            raise ValueError("ffmpeg produced no output")

        yield {"progress": 0.9, "stage": "verifying output"}
        meta = await asyncio.to_thread(
            engine.probe, out_path, cancel=handle.cancel_event)
        if not meta.get("ok"):
            out_path.unlink(missing_ok=True)
            raise ValueError("export output failed verification")

        yield {"progress": 1.0, "done": True, "output": {
            "path": str(out_path),
            "kind": "video",
            "origin": "rendered",
            "name": f"{req.get('timeline_name') or 'timeline'} export.mp4",
            "media": {**meta, "render": {
                "path": path_kind, "reason": reason,
                "profile": profile_name, "crf": crf,
                "edl_sha256": req.get("edl_sha256")}},
        }}
