/**
 * A2 — shot planning controls for the storyboard shot dialog, plus the
 * shot-scoped CAS media section. Vocabulary mirrors the backend exactly
 * (app/domain/storyboard.py) — the ten camera-follow modes are the spec
 * vocabulary, never invent aliases.
 */
import { useState } from "react";
import type { Entity, ReferenceSet, ShotPlanning } from "../../api/types";

export const CAMERA_ANGLES = [
  ["", "—"], ["eye_level", "Eye level"], ["low", "Low"], ["high", "High"],
  ["top_down", "Top-down"], ["dutch", "Dutch"],
] as const;
export const CAMERA_MOVEMENTS = [
  ["", "—"], ["static", "Static"], ["pan", "Pan"], ["tilt", "Tilt"],
  ["dolly_in", "Dolly in"], ["dolly_out", "Dolly out"],
  ["truck_left", "Truck left"], ["truck_right", "Truck right"],
  ["crane_up", "Crane up"], ["crane_down", "Crane down"],
  ["handheld", "Handheld"], ["zoom", "Zoom"],
] as const;
export const FOLLOW_MODES = [
  ["", "—"], ["follow_behind", "Follow behind"], ["follow_front", "Follow front"],
  ["follow_side", "Follow side"], ["follow_shoulder", "Follow shoulder"],
  ["orbit_follow", "Orbit follow"], ["drone_follow", "Drone follow"],
  ["vehicle_chase", "Vehicle chase"], ["lead_and_follow", "Lead & follow"],
  ["group_follow", "Group follow"], ["target_switch", "Target switch"],
] as const;
export const FRAMINGS = [
  ["", "—"], ["extreme_close_up", "Extreme close-up"], ["close_up", "Close-up"],
  ["medium_close_up", "Medium close-up"], ["medium", "Medium"],
  ["medium_wide", "Medium wide"], ["wide", "Wide"], ["extreme_wide", "Extreme wide"],
] as const;
export const PLACEMENTS = [
  ["", "—"], ["center", "Center"], ["left_third", "Left third"],
  ["right_third", "Right third"], ["upper_third", "Upper third"],
  ["lower_third", "Lower third"],
] as const;

export const FOLLOW_SHORT: Record<string, string> = {
  follow_behind: "behind", follow_front: "front", follow_side: "side",
  follow_shoulder: "shoulder", orbit_follow: "orbit", drone_follow: "drone",
  vehicle_chase: "chase", lead_and_follow: "lead", group_follow: "group",
  target_switch: "switch",
};

type Target = { kind: "character"; id: string } | { kind: "label"; name: string };
type CRef = { kind: string; id: string };

/** Flat editable draft — strings so empty means "unset" (→ omit/clear). */
export interface PlanDraft {
  angle: string; movement: string; followMode: string;
  framing: string; placement: string;
  lighting: string; action: string; notes: string;
  fpsNum: string; fpsDen: string; fpsFrames: string;
  followDistance: string; followHeight: string; followLateral: string;
  followTargets: Target[];
  continuityRefs: CRef[];
}

export function draftFromPlanning(p?: ShotPlanning | null): PlanDraft {
  const cam = p?.camera ?? {};
  const prm = cam.follow_params ?? {};
  const num = (k: string) =>
    prm[k] !== undefined && prm[k] !== null ? String(prm[k]) : "";
  return {
    angle: cam.angle ?? "", movement: cam.movement ?? "",
    followMode: cam.follow_mode ?? "",
    framing: p?.framing ?? "", placement: p?.subject_placement ?? "",
    lighting: p?.lighting ?? "", action: p?.action_notes ?? "",
    notes: p?.notes ?? "",
    fpsNum: p?.fps ? String(p.fps.num) : "",
    fpsDen: p?.fps ? String(p.fps.den) : "",
    fpsFrames: p?.fps?.frames !== undefined ? String(p.fps.frames) : "",
    followDistance: num("distance_m"), followHeight: num("height_m"),
    followLateral: num("lateral_m"),
    followTargets: (cam.follow_targets ?? []) as Target[],
    continuityRefs: p?.continuity_refs ?? [],
  };
}

/** Serialize the draft into a merge-doc: changed fields only, "" → null
 * (explicit clear) when the original held a value, omitted otherwise. */
export function planningFromDraft(d: PlanDraft, orig: PlanDraft): Record<string, unknown> {
  const out: Record<string, unknown> = {};
  const cam: Record<string, unknown> = {};
  const field = (draft: string, o: string) => {
    if (draft === o) return;
    return draft.trim() ? draft.trim() : null;
  };
  const f = (k: "angle" | "movement" | "follow_mode",
             cur: string, was: string) => {
    if (cur === was) return;
    cam[k] = cur || null;
  };
  f("angle", d.angle, orig.angle);
  f("movement", d.movement, orig.movement);
  f("follow_mode", d.followMode, orig.followMode);
  const prm: Record<string, number> = {};
  const addNum = (k: string, v: string) => {
    const n = parseFloat(v);
    if (v.trim() && !Number.isNaN(n)) prm[k] = n;
  };
  addNum("distance_m", d.followDistance);
  addNum("height_m", d.followHeight);
  addNum("lateral_m", d.followLateral);
  const oPrm: Record<string, number> = {};
  addNumTo(oPrm, "distance_m", orig.followDistance);
  addNumTo(oPrm, "height_m", orig.followHeight);
  addNumTo(oPrm, "lateral_m", orig.followLateral);
  if (JSON.stringify(prm) !== JSON.stringify(oPrm)) {
    cam.follow_params = Object.keys(prm).length ? prm : null;
  }
  if (JSON.stringify(d.followTargets) !== JSON.stringify(orig.followTargets)) {
    cam.follow_targets = d.followTargets;
  }
  if (Object.keys(cam).length) out.camera = cam;
  const g = (k: "framing" | "subject_placement", cur: string, was: string) => {
    if (cur !== was) out[k] = cur || null;
  };
  g("framing", d.framing, orig.framing);
  g("subject_placement", d.placement, orig.placement);
  for (const [k, cur, was] of [
    ["lighting", d.lighting, orig.lighting],
    ["action_notes", d.action, orig.action],
    ["notes", d.notes, orig.notes],
  ] as const) {
    const v = field(cur, was);
    if (v !== undefined) out[k] = v;
  }
  const fpsEmpty = !d.fpsNum.trim() && !d.fpsDen.trim();
  const oFpsEmpty = !orig.fpsNum.trim() && !orig.fpsDen.trim();
  if (!fpsEmpty || !oFpsEmpty) {
    if (fpsEmpty !== oFpsEmpty || d.fpsNum !== orig.fpsNum
        || d.fpsDen !== orig.fpsDen || d.fpsFrames !== orig.fpsFrames) {
      out.fps = fpsEmpty
        ? null
        : { num: parseInt(d.fpsNum, 10) || 24,
            den: parseInt(d.fpsDen, 10) || 1,
            ...(d.fpsFrames.trim() !== ""
                ? { frames: Math.max(0, parseInt(d.fpsFrames, 10) || 0) } : {}) };
    }
  }
  if (JSON.stringify(d.continuityRefs) !== JSON.stringify(orig.continuityRefs)) {
    out.continuity_refs = d.continuityRefs;
  }
  return out;
}
function addNumTo(o: Record<string, number>, k: string, v: string) {
  const n = parseFloat(v);
  if (v.trim() && !Number.isNaN(n)) o[k] = n;
}

const SEL = "w-full rounded border border-line bg-ink px-2 py-1 text-xs";
const INP = SEL;
const LBL = "text-[10px] uppercase tracking-wide text-faint";

export function PlanningSection({ value, onChange, characters, locations, assets, refSets }: {
  value: PlanDraft;
  onChange: (d: PlanDraft) => void;
  characters: Entity[];
  locations: Entity[];
  assets: { id: string; name: string }[];
  refSets: ReferenceSet[];
}) {
  const set = (patch: Partial<PlanDraft>) => onChange({ ...value, ...patch });
  const [newLabel, setNewLabel] = useState("");
  const [crefKind, setCrefKind] = useState("character");
  const [crefId, setCrefId] = useState("");
  const charName = (id: string) => characters.find((c) => c.id === id)?.name ?? "?";
  const crefOptions: Record<string, { id: string; name: string }[]> = {
    character: characters.map((c) => ({ id: c.id, name: c.name })),
    location: locations.map((l) => ({ id: l.id, name: l.name })),
    asset: assets,
    refset: refSets.map((r) => ({ id: r.id, name: r.name })),
  };
  const crefName = (r: CRef) =>
    crefOptions[r.kind]?.find((o) => o.id === r.id)?.name ?? `${r.kind} (missing)`;

  return (
    <section className="mt-4 rounded-md border border-line bg-ink/40 p-3">
      <h4 className="text-xs font-semibold text-fg">Shot planning</h4>
      <p className="mt-0.5 text-[10px] text-muted">
        Camera and blocking intent — kept when the screenplay re-syncs.
      </p>
      <div className="mt-2 grid grid-cols-3 gap-2">
        <label className="block"><span className={LBL}>Camera angle</span>
          <select className={SEL} value={value.angle}
            onChange={(e) => set({ angle: e.target.value })}>
            {CAMERA_ANGLES.map(([v, l]) => <option key={v} value={v}>{l}</option>)}
          </select></label>
        <label className="block"><span className={LBL}>Movement</span>
          <select className={SEL} value={value.movement}
            onChange={(e) => set({ movement: e.target.value })}>
            {CAMERA_MOVEMENTS.map(([v, l]) => <option key={v} value={v}>{l}</option>)}
          </select></label>
        <label className="block"><span className={LBL}>Follow / tracking</span>
          <select className={SEL} value={value.followMode}
            onChange={(e) => set({ followMode: e.target.value })}>
            {FOLLOW_MODES.map(([v, l]) => <option key={v} value={v}>{l}</option>)}
          </select></label>
        <label className="block"><span className={LBL}>Framing</span>
          <select className={SEL} value={value.framing}
            onChange={(e) => set({ framing: e.target.value })}>
            {FRAMINGS.map(([v, l]) => <option key={v} value={v}>{l}</option>)}
          </select></label>
        <label className="block"><span className={LBL}>Subject placement</span>
          <select className={SEL} value={value.placement}
            onChange={(e) => set({ placement: e.target.value })}>
            {PLACEMENTS.map(([v, l]) => <option key={v} value={v}>{l}</option>)}
          </select></label>
        <label className="block"><span className={LBL}>Lighting note</span>
          <input className={INP} value={value.lighting} maxLength={2000}
            placeholder="e.g. moon + practical"
            onChange={(e) => set({ lighting: e.target.value })} /></label>
      </div>
      <label className="mt-2 block"><span className={LBL}>Action notes</span>
        <input className={INP} value={value.action} maxLength={2000}
          placeholder="Blocking / performance intent"
          onChange={(e) => set({ action: e.target.value })} /></label>
      <label className="mt-2 block"><span className={LBL}>Planner notes</span>
        <input className={INP} value={value.notes} maxLength={2000}
          placeholder="Anything else for this shot"
          onChange={(e) => set({ notes: e.target.value })} /></label>

      <details className="mt-3">
        <summary className="cursor-pointer text-[11px] text-teal">
          Advanced — timing, follow targets &amp; continuity refs
        </summary>
        <div className="mt-2 space-y-2">
          <div className="grid grid-cols-4 gap-2">
            <label className="block"><span className={LBL}>fps num</span>
              <input className={INP} inputMode="numeric" value={value.fpsNum}
                placeholder="24000" onChange={(e) => set({ fpsNum: e.target.value })} /></label>
            <label className="block"><span className={LBL}>fps den</span>
              <input className={INP} inputMode="numeric" value={value.fpsDen}
                placeholder="1001" onChange={(e) => set({ fpsDen: e.target.value })} /></label>
            <label className="block"><span className={LBL}>frames</span>
              <input className={INP} inputMode="numeric" value={value.fpsFrames}
                placeholder="96" onChange={(e) => set({ fpsFrames: e.target.value })} /></label>
            <div className="self-end pb-1 text-[10px] text-muted">
              rational frame rate (e.g. 24000/1001)
            </div>
          </div>
          {value.followMode && (
            <div className="grid grid-cols-3 gap-2">
              <label className="block"><span className={LBL}>distance m</span>
                <input className={INP} inputMode="decimal" value={value.followDistance}
                  onChange={(e) => set({ followDistance: e.target.value })} /></label>
              <label className="block"><span className={LBL}>height m</span>
                <input className={INP} inputMode="decimal" value={value.followHeight}
                  onChange={(e) => set({ followHeight: e.target.value })} /></label>
              <label className="block"><span className={LBL}>lateral m</span>
                <input className={INP} inputMode="decimal" value={value.followLateral}
                  onChange={(e) => set({ followLateral: e.target.value })} /></label>
            </div>
          )}
          <div>
            <span className={LBL}>Follow targets</span>
            <div className="mt-1 flex flex-wrap items-center gap-1.5">
              {value.followTargets.map((t, i) => (
                <span key={i}
                  className="flex items-center gap-1 rounded bg-panel px-1.5 py-0.5 text-[10px] text-fg">
                  {t.kind === "character" ? `◉ ${charName(t.id)}` : `◇ ${t.name}`}
                  <button type="button" aria-label="Remove follow target"
                    onClick={() => set({ followTargets: value.followTargets.filter((_, j) => j !== i) })}
                    className="text-faint hover:text-danger">✕</button>
                </span>
              ))}
              <select
                aria-label="Add character follow target"
                className="rounded border border-line bg-ink px-1.5 py-0.5 text-[10px]"
                value=""
                onChange={(e) => {
                  if (e.target.value) set({ followTargets: [...value.followTargets, { kind: "character", id: e.target.value }] });
                }}>
                <option value="">+ character…</option>
                {characters.filter((c) => c.status === "active")
                  .map((c) => <option key={c.id} value={c.id}>{c.name}</option>)}
              </select>
              <input
                aria-label="Label follow target"
                className="w-28 rounded border border-line bg-ink px-1.5 py-0.5 text-[10px]"
                placeholder="or label (e.g. car)" value={newLabel}
                onChange={(e) => setNewLabel(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === "Enter" && newLabel.trim()) {
                    set({ followTargets: [...value.followTargets, { kind: "label", name: newLabel.trim() }] });
                    setNewLabel("");
                  }
                }} />
            </div>
          </div>
          <div>
            <span className={LBL}>Continuity references</span>
            <div className="mt-1 flex flex-wrap items-center gap-1.5">
              {value.continuityRefs.map((r, i) => (
                <span key={i}
                  className="flex items-center gap-1 rounded bg-panel px-1.5 py-0.5 text-[10px] text-fg">
                  {r.kind}: {crefName(r)}
                  <button type="button" aria-label="Remove continuity reference"
                    onClick={() => set({ continuityRefs: value.continuityRefs.filter((_, j) => j !== i) })}
                    className="text-faint hover:text-danger">✕</button>
                </span>
              ))}
              <select aria-label="Continuity reference kind"
                className="rounded border border-line bg-ink px-1.5 py-0.5 text-[10px]"
                value={crefKind} onChange={(e) => { setCrefKind(e.target.value); setCrefId(""); }}>
                <option value="character">character</option>
                <option value="location">location</option>
                <option value="asset">asset</option>
                <option value="refset">ref set</option>
              </select>
              <select aria-label="Continuity reference target"
                className="rounded border border-line bg-ink px-1.5 py-0.5 text-[10px]"
                value={crefId} onChange={(e) => setCrefId(e.target.value)}>
                <option value="">pick…</option>
                {(crefOptions[crefKind] ?? []).map((o) => (
                  <option key={o.id} value={o.id}>{o.name}</option>))}
              </select>
              <button type="button" disabled={!crefId}
                onClick={() => {
                  set({ continuityRefs: [...value.continuityRefs, { kind: crefKind, id: crefId }] });
                  setCrefId("");
                }}
                className="rounded border border-line px-1.5 py-0.5 text-[10px] text-muted hover:text-teal disabled:opacity-40">
                + Add
              </button>
            </div>
          </div>
        </div>
      </details>
    </section>
  );
}

const MEDIA_ROLES = [
  ["reference", "Reference"], ["style", "Style"], ["identity", "Identity"],
  ["wardrobe", "Wardrobe"], ["voice", "Voice"],
] as const;

/** A2 — shot-scoped CAS media. Existing project images only — no
 * generation here; missing blobs are flagged for relinking. */
export function ShotMediaSection({ entry, imageAssets, run }: {
  entry: { shot: { id: string }; media?: { assets: {
    link_id: string; asset_id: string; role: string; label: string;
    asset_name: string; asset_status: string; missing: boolean;
  }[]; missing: string[] } };
  imageAssets: { id: string; name: string }[];
  run: (t: string, p: Record<string, unknown>) => Promise<boolean>;
}) {
  const [pick, setPick] = useState("");
  const [role, setRole] = useState("reference");
  const [label, setLabel] = useState("");
  const [err, setErr] = useState<string | null>(null);
  const links = entry.media?.assets ?? [];
  const linked = new Set(links.map((l) => l.asset_id));
  const avail = imageAssets.filter((a) => !linked.has(a.id));

  async function act(t: string, p: Record<string, unknown>) {
    setErr(null);
    if (!(await run(t, p))) setErr("Media change failed — see the board message.");
  }

  return (
    <section className="mt-4 rounded-md border border-line bg-ink/40 p-3">
      <h4 className="text-xs font-semibold text-fg">Board media</h4>
      <p className="mt-0.5 text-[10px] text-muted">
        Existing project images pinned to this shot — references, boards,
        continuity stills. Missing files show up here for relinking.
      </p>
      {err && <p className="mt-1 text-[11px] text-danger">{err}</p>}
      <ul className="mt-2 space-y-1">
        {links.map((l) => (
          <li key={l.link_id}
            className="flex items-center gap-2 rounded border border-line bg-panel px-2 py-1 text-[11px]">
            <span className={`font-medium ${l.missing ? "text-amber" : "text-fg"}`}>
              {l.missing ? "⚠ " : ""}{l.asset_name}
            </span>
            <span className="text-faint">{l.role}</span>
            {l.label && <span className="truncate text-muted">· {l.label}</span>}
            {l.missing && (
              <span className="text-[10px] text-amber">
                {l.asset_status === "gone" ? "asset gone" : l.asset_status} — relink or remove
              </span>
            )}
            <button type="button"
              onClick={() => void act("entity.assets.unlink", { link_id: l.link_id })}
              aria-label={`Remove ${l.asset_name}`}
              className="ml-auto text-faint hover:text-danger">✕</button>
          </li>
        ))}
        {links.length === 0 && (
          <li className="text-[11px] text-faint">No media pinned yet.</li>
        )}
      </ul>
      <div className="mt-2 flex items-center gap-1.5">
        <select aria-label="Pick media asset" className={SEL + " flex-1"}
          value={pick} onChange={(e) => setPick(e.target.value)}>
          <option value="">Pick an image asset…</option>
          {avail.map((a) => <option key={a.id} value={a.id}>{a.name}</option>)}
        </select>
        <select aria-label="Media role" className={SEL + " w-28"}
          value={role} onChange={(e) => setRole(e.target.value)}>
          {MEDIA_ROLES.map(([v, l]) => <option key={v} value={v}>{l}</option>)}
        </select>
        <input aria-label="Media label" className={INP + " w-24"}
          placeholder="label" value={label}
          onChange={(e) => setLabel(e.target.value)} />
        <button type="button" disabled={!pick}
          onClick={() => {
            void act("entity.assets.link", {
              entity_type: "shot", entity_id: entry.shot.id,
              asset_id: pick, role, label: label.trim(),
            });
            setPick(""); setLabel("");
          }}
          className="rounded border border-line px-2 py-1 text-[11px] text-muted hover:text-teal disabled:opacity-40">
          Pin
        </button>
      </div>
    </section>
  );
}
