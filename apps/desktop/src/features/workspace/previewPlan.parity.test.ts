/**
 * N2-F review §1 — shared-fixture preview/render parity (frontend side).
 *
 * Loads the SAME language-neutral fixtures as
 * backend/tests/test_n2f_parity.py — `fixtures/audio-parity/*.json`.
 * That suite drives the fixture through `composite_plan` + the real
 * `_lane_chain` ffmpeg render; this one drives it through
 * `buildPreviewPlan` and env(). Both must satisfy independently-
 * derived timing and gain constants — no formula is duplicated.
 */
import { describe, expect, it } from "vitest";
import { readFileSync, readdirSync } from "node:fs";
import { join } from "node:path";
import { buildPreviewPlan } from "./previewPlan";
import type {
  TimelineClip, TimelineCrossfade, TimelineDetail, TimelineTrack,
} from "../../api/types";

// vitest cwd is apps/desktop — repo root is two levels up.
const DIR = join(process.cwd(), "..", "..", "fixtures", "audio-parity")
  + "/";
const TOL = 3e-3;
const FIELD_TOL = 1e-4;

interface Fx {
  name: string;
  tl_fps: { num: number; den: number };
  tracks: (Partial<TimelineTrack> & {
    automation?: { at_tl_frames: number; gain_db: number }[];
  })[];
  clips: (Partial<TimelineClip> & {
    id: string; track_id: string; src_fps: { num: number; den: number };
    automation?: { at_frames: number; gain_db: number }[];
  })[];
  crossfades?: Partial<TimelineCrossfade>[];
  expect: {
    detached_xfs: string[];
    clip_ids: string[];
    clips: Record<string, {
      tl_start_s: number; tl_end_s: number;
      src_start_s: number; seg_dur_s: number;
      embedded?: boolean;
      env?: { u: number; g: number }[];
    }>;
  };
}

function toDetail(fx: Fx): TimelineDetail {
  const tlFps = fx.tl_fps.num / fx.tl_fps.den;
  const tracks: TimelineTrack[] = fx.tracks.map((t, i) => ({
    id: t.id ?? `t${i}`, project_id: "p", timeline_id: "tl",
    kind: t.kind ?? "audio", name: t.id ?? `t${i}`, position: i,
    visible: t.visible ?? true, locked: false,
    muted: t.muted ?? false, gain_db: t.gain_db ?? 0,
    solo: t.solo ?? false, created_at: "2026-01-01",
    automation_json: JSON.stringify(
      (t.automation ?? []).map((p, k) => ({ id: `k${k}`, ...p }))),
    automation_interp: t.automation_interp ?? "linear",
  }));
  const clips: TimelineClip[] = fx.clips.map((c, i) => {
    const rate = c.src_fps.num / c.src_fps.den;
    return {
      id: c.id, project_id: "p", timeline_id: "tl",
      track_id: c.track_id, position: i,
      start_frames: c.start_frames ?? 0,
      source_asset_id: c.asset_id ?? "a",
      source_version_id: "v",
      source_sha256: c.sha256 ?? "ab".repeat(32),
      in_frame: c.in_frame ?? 0, out_frame: c.out_frame ?? 0,
      src_frames: c.src_frames ?? c.out_frame ?? 0,
      src_fps_num: c.src_fps.num, src_fps_den: c.src_fps.den,
      src_duration_s: (c.out_frame ?? 0) / rate,
      src_has_audio: c.src_has_audio ?? true,
      gain_db: c.gain_db ?? 0,
      fade_in_units: c.fade_in_units ?? 0,
      fade_in_curve: c.fade_in_curve ?? "linear",
      fade_out_units: c.fade_out_units ?? 0,
      fade_out_curve: c.fade_out_curve ?? "linear",
      automation_json: JSON.stringify(
        (c.automation ?? []).map((p, k) => ({ id: `k${k}`, ...p }))),
      automation_interp: c.automation_interp ?? "linear",
      shot_id: null, scene_id: null, created_at: "2026-01-01",
    };
  });
  const crossfades: TimelineCrossfade[] = (fx.crossfades ?? [])
    .map((x, i) => ({
      id: x.id ?? `x${i}`, project_id: "p", timeline_id: "tl",
      out_clip_id: x.out_clip_id ?? "", in_clip_id: x.in_clip_id ?? "",
      tl_start: x.tl_start ?? 0, tl_frames: x.tl_frames ?? 0,
      curve: x.curve ?? "equal_power",
      satisfied: x.satisfied ?? true, created_at: "2026-01-01",
    }));
  const end = Math.max(0, ...clips.map(c => {
    const rate = c.src_fps_num / c.src_fps_den || tlFps;
    return c.start_frames
      + (c.out_frame - c.in_frame) * tlFps / rate;
  }));
  return {
    id: "tl", project_id: "p", name: "fx", description: "",
    fps_num: fx.tl_fps.num, fps_den: fx.tl_fps.den,
    status: "active", revision: 1,
    created_at: "2026-01-01", updated_at: "2026-01-01",
    clips, tracks, crossfades,
    total_duration_s: end / tlFps,
    undo_label: null, redo_label: null,
  } as TimelineDetail;
}

const files = readdirSync(DIR).filter(f => f.endsWith(".json")).sort();

describe("audio-parity fixtures (shared with backend)", () => {
  for (const f of files) {
    it(f.replace(/\.json$/, ""), () => {
      const fx = JSON.parse(readFileSync(DIR + f, "utf8")) as Fx;
      const plan = buildPreviewPlan(toDetail(fx));
      const exp = fx.expect;
      expect(plan.detachedXfs.sort()).toEqual(
        [...exp.detached_xfs].sort());
      expect(plan.clips.map(c => c.clipId).sort())
        .toEqual([...exp.clip_ids].sort());
      for (const [cid, e] of Object.entries(exp.clips)) {
        const c = plan.clips.find(x => x.clipId === cid);
        expect(c, `${f}:${cid} missing`).toBeTruthy();
        if (!c) continue;
        expect(c.tlStartS, `${f}:${cid}.tlStartS`)
          .toBeCloseTo(e.tl_start_s, 4);
        expect(c.tlEndS, `${f}:${cid}.tlEndS`)
          .toBeCloseTo(e.tl_end_s, 4);
        expect(c.srcStartS, `${f}:${cid}.srcStartS`)
          .toBeCloseTo(e.src_start_s, 4);
        expect(c.segDurS, `${f}:${cid}.segDurS`)
          .toBeCloseTo(e.seg_dur_s, 4);
        if (e.embedded !== undefined) {
          expect(c.embedded, `${f}:${cid}.embedded`).toBe(e.embedded);
        }
        for (const cp of e.env ?? []) {
          // Checkpoints are stream-local SOURCE units; env() takes
          // stream-local seconds (embedded: seconds too, constant).
          const t = c.embedded ? cp.u / 48000 : cp.u / c.rate;
          expect(Math.abs(c.env(t) - cp.g),
            `${f}:${cid} env(${cp.u}) ${c.env(t)} != ${cp.g}`)
            .toBeLessThan(TOL + FIELD_TOL);
        }
      }
    });
  }
});
