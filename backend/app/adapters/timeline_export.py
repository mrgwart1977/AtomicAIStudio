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
import os
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
        fmt = edl.get("format")
        if fmt not in ("ade-edl/1", "ade-edl/2"):
            errs.append("edl must be an ade-edl/1 or ade-edl/2 document")
        elif not edl.get("clips"):
            errs.append("timeline has no clips to export")
        elif fmt == "ade-edl/2":
            try:
                plan = tx.composite_plan(edl, {})
            except ValueError as exc:
                errs.append(str(exc))
            else:
                if not plan["clips"] and not plan["audio_clips"]:
                    errs.append("no visible lane holds clips — nothing "
                                "to composite")
                elif plan["program_frames"] <= 0:
                    errs.append("composite program has zero duration")
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
        is_v2 = edl.get("format") == "ade-edl/2"
        yield {"progress": 0.05, "stage": "resolving sources"}

        with self._sf() as s:
            paths = [self._source_path(s, c["sha256"], c["clip_id"])
                     for c in clips]
        # Verify pinned bytes + probe off the event loop — hashing and
        # ffprobe are blocking work that would stall the whole backend
        # (and every cancel request) for the duration of a render.
        seen: set[str] = set()
        for p, c in zip(paths, clips):
            if c["sha256"] not in seen:  # one hash per unique blob
                await asyncio.to_thread(
                    self._verify_bytes, p, c["sha256"], c["clip_id"])
                seen.add(c["sha256"])
        handle.check_cancel()

        yield {"progress": 0.12, "stage": "probing sources"}
        track_kind = {t["id"]: t.get("kind")
                      for t in edl.get("tracks") or []}
        # Per-source ffprobe is a ~1s process spawn on Windows — serial
        # probing dominated large-timeline exports (~25 min at 1000
        # sources, measured). Bounded-concurrent probing keeps order
        # deterministic (gather preserves input order) while cutting
        # wall time ~8x; the cancel_event still aborts each probe.
        sem = asyncio.Semaphore(8)

        async def _probe_one(c, p):
            # Audio-lane sources are audio-kind files — engine.probe
            # requires a video stream, so they take the audio prober.
            prober = (engine.probe_audio
                      if track_kind.get(c.get("track_id")) == "audio"
                      else engine.probe)
            async with sem:
                return await asyncio.to_thread(
                    prober, p, cancel=handle.cancel_event)

        probes = await asyncio.gather(
            *[_probe_one(c, p) for c, p in zip(clips, paths)])
        for c, p, meta in zip(clips, paths, probes):
            if not meta.get("ok"):
                raise ValueError(
                    f"probe failed for {p.name}: {meta.get('reason')}")
        handle.check_cancel()

        if is_v2:
            probe_by_clip = {c["clip_id"]: m
                             for c, m in zip(clips, probes)}
            plan = tx.composite_plan(edl, probe_by_clip)
            for c in plan["clips"]:
                if not (probe_by_clip.get(c["clip_id"]) or {}) \
                        .get("codec"):
                    raise ValueError(
                        f"clip {c['clip_id']} has no decodable video "
                        "stream — cannot composite")
            path_kind, reason = "composite", "ade-edl/2 multitrack"
        else:
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
        # The filter graph travels in a file under the job work dir —
        # never on the command line (Windows' ~32k argv limit fails
        # large timelines) and never in a shared temp location.
        script_path = work / f"{handle.job_id}-filter.txt"
        # Each UNIQUE source also enters argv once per -i; the
        # single-pass paths hardlink sources into the job dir as short
        # aliases (~40 chars) so the argv bound sits around ~600-750
        # sources — ade-edl/2 timelines beyond STAGE_MAX_INPUTS route
        # to the staged submix instead (bounded argv at ANY count).
        # The legacy single-lane encode path keeps its own argv bound
        # and refuses preflight with an actionable error (below).
        # Fallback to the absolute path when linking isn't possible.
        alias: dict[str, str] = {}

        def short_src(p: str) -> str:
            if p not in alias:
                link = work / f"in{len(alias)}{Path(p).suffix}"
                try:
                    os.link(p, link)
                    alias[p] = str(link)
                except OSError:
                    alias[p] = p
            return alias[p]
        r = None
        try:
            if is_v2:
                if not plan["clips"] and not plan["audio_clips"]:
                    raise ValueError(
                        "no visible lane holds clips — nothing to composite")
                target = tx.composite_canvas(plan, profile_name,
                                             probe_by_clip)
                enc = await asyncio.to_thread(
                    tx.h264_encoder, ffmpeg["path"], supervise.run)
                clip_paths = {c["clip_id"]: short_src(str(p))
                              for c, p in zip(clips, paths)}
                stages = tx.stage_windows(
                    plan, clip_paths,
                    int(os.environ.get("ADE_STAGE_MAX_INPUTS") or 0)
                    or None)
                if len(stages) <= 1:
                    argv, graph = tx.build_composite_argv(
                        ffmpeg["path"], plan, clip_paths, target, crf,
                        str(out_path), encoder=enc,
                        script_path=str(script_path))
                    argv_len = sum(len(a) + 1 for a in argv)
                    if argv_len > 30000:
                        raise ValueError(
                            f"export argv is {argv_len} chars — "
                            "exceeds the single-pass composite bound "
                            "(~30,000 chars on Windows). Long source "
                            "paths defeated alias shortening; lower "
                            "ADE_STAGE_MAX_INPUTS to force the staged "
                            "path or shorten CAS paths.")
                    script_path.write_text(graph, encoding="utf-8")
                else:
                    # C3 staged submix: per-window lossless intermediates,
                    # concat-demuxer final encode — argv, handles, and
                    # memory all bounded regardless of source count.
                    yield {"progress": 0.3, "stage":
                           f"staged render ({len(stages)} windows)"}
                    interm: list[Path] = []
                    list_path = work / f"{handle.job_id}-concat.txt"
                    try:
                        for si, (F, M) in enumerate(stages):
                            handle.check_cancel()
                            sub = tx.stage_subplan(plan, F, M)
                            ipath = (work /
                                     f"{handle.job_id}-stage{si}.mkv")
                            gpath = work / f"{handle.job_id}-g{si}.txt"
                            sargv, sgraph = tx.build_stage_argv(
                                ffmpeg["path"], sub, clip_paths,
                                target, str(ipath), str(gpath))
                            gpath.write_text(sgraph, encoding="utf-8")
                            r = await asyncio.to_thread(
                                supervise.run, sargv,
                                timeout_s=timeout_s,
                                cancel=handle.cancel_event)
                            gpath.unlink(missing_ok=True)
                            if r["cancelled"]:
                                raise JobCancelled()
                            if r["exit"] != 0 or not ipath.is_file():
                                ipath.unlink(missing_ok=True)
                                tail = (r["output_tail"] or
                                        "ffmpeg failed").strip()[-300:]
                                raise ValueError(
                                    f"stage {si} render failed: {tail}")
                            interm.append(ipath)
                            yield {"progress":
                                   0.3 + 0.45 * (si + 1) / len(stages),
                                   "stage": f"window {si + 1}/"
                                            f"{len(stages)}"}
                        list_path.write_text(
                            tx.concat_list([str(p) for p in interm]),
                            encoding="utf-8")
                        dur_s = float(plan["program_frames"]
                                      / Fraction(target["fps_num"],
                                                 target["fps_den"]))
                        fargv = tx.build_final_argv(
                            ffmpeg["path"], str(list_path), dur_s,
                            str(out_path), enc, crf,
                            has_audio=bool(plan["audio_clips"]))
                        r = await asyncio.to_thread(
                            supervise.run, fargv, timeout_s=timeout_s,
                            cancel=handle.cancel_event)
                    finally:
                        for p_ in interm:
                            p_.unlink(missing_ok=True)
                        list_path.unlink(missing_ok=True)
            elif path_kind == "copy":
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
                    segments.append({"path": short_src(str(p)),
                                     "in_s": in_s, "dur_s": dur_s,
                                     "has_audio": bool(meta.get("audio"))})
                argv, graph = tx.build_encode_argv(
                    ffmpeg["path"], segments, target, crf, str(out_path),
                    encoder=enc, script_path=str(script_path))
                # F6 preflight: the single-pass encode emits one -i per
                # clip — refuse BEFORE rendering when argv would
                # overflow CreateProcess (~32,767 chars; keep margin).
                # Actionable: a multitrack (ade-edl/2) timeline routes
                # to the staged path instead.
                argv_len = sum(len(a) + 1 for a in argv)
                if argv_len > 30000:
                    raise ValueError(
                        f"export argv is {argv_len} chars with "
                        f"{len(segments)} input(s) — exceeds the "
                        "single-pass encode bound (~30,000 chars on "
                        "Windows). Split the export into smaller "
                        "timeline ranges, or rebuild the program on "
                        "multiple tracks so the staged export path "
                        "applies.")
                script_path.write_text(graph, encoding="utf-8")
            if r is None:
                r = await asyncio.to_thread(
                    supervise.run, argv, timeout_s=timeout_s,
                    cancel=handle.cancel_event)
        finally:
            script_path.unlink(missing_ok=True)
            for orig, link in alias.items():
                if link != orig:
                    Path(link).unlink(missing_ok=True)
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
