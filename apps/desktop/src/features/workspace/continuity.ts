// C4-C — lightweight continuity comparison between neighboring shots.
// Pure string/metadata diff over continuity_json + cast state — no CV,
// no provider calls, and honest wording: it surfaces differences a
// human should check, never a claim of visual consistency.
import type { CastMember, StoryboardShot } from "../../api/types";

/** Known continuity_json keys the UI writes — descriptive guidance only. */
export const CONTINUITY_KEYS = [
  { key: "appearance", label: "Character appearance" },
  { key: "wardrobe", label: "Wardrobe" },
  { key: "location", label: "Location detail" },
  { key: "lighting", label: "Lighting" },
  { key: "camera", label: "Camera framing" },
  { key: "mood", label: "Mood & atmosphere" },
  { key: "notes", label: "Stays consistent" },
  { key: "changes", label: "What changed" },
] as const;

export interface ContinuityDiff {
  /** Characters present in BOTH shots (own ∪ inherited). */
  sharedCharacters: string[];
  /** Characters only in the previous/next shot. */
  onlyPrev: string[];
  onlyNext: string[];
  /** Per-field differences: label + prev/next values ("" = unset). */
  fields: { label: string; prev: string; next: string }[];
  /** Wardrobe/expression differences from cast state rows. */
  castChanges: { name: string; prev: string; next: string }[];
  locationChanged: boolean;
  prevLocation: string;
  nextLocation: string;
}

function castIdsOf(e: StoryboardShot): Map<string, CastMember> {
  const m = new Map<string, CastMember>();
  for (const c of e.cast) m.set(c.character_id, c);
  for (const c of e.inherited ?? []) m.set(c.character_id, c);
  return m;
}

function cont(e: StoryboardShot, key: string): string {
  const v = (e.shot.continuity_json || {})[key];
  return v == null ? "" : String(v);
}

/** Shared-cast wardrobe/expression diffs worth a human look. */
function castDiffs(prev: StoryboardShot, next: StoryboardShot,
                   shared: Set<string>) {
  const pm = castIdsOf(prev);
  const nm = castIdsOf(next);
  const out: { name: string; prev: string; next: string }[] = [];
  for (const id of shared) {
    const p = pm.get(id)!;
    const n = nm.get(id)!;
    const name = n.character?.name ?? p.character?.name ?? "?";
    for (const k of ["wardrobe_variant", "expression", "emotion"] as const) {
      const a = String(p[k] ?? ""), b = String(n[k] ?? "");
      if (a !== b && (a || b)) {
        out.push({ name, prev: `${k}: ${a || "—"}`,
                   next: `${k}: ${b || "—"}` });
      }
    }
  }
  return out;
}

export function compareShots(prev: StoryboardShot, next: StoryboardShot): ContinuityDiff {
  const pm = castIdsOf(prev);
  const nm = castIdsOf(next);
  const shared = [...pm.keys()].filter((id) => nm.has(id));
  const sharedSet = new Set(shared);
  const fields = CONTINUITY_KEYS
    .map(({ key, label }) => ({ label, prev: cont(prev, key), next: cont(next, key) }))
    .filter((f) => f.prev !== f.next && (f.prev || f.next));
  return {
    sharedCharacters: shared.map((id) => nm.get(id)?.character?.name ?? "?"),
    onlyPrev: [...pm.keys()].filter((id) => !nm.has(id))
      .map((id) => pm.get(id)?.character?.name ?? "?"),
    onlyNext: [...nm.keys()].filter((id) => !pm.has(id))
      .map((id) => nm.get(id)?.character?.name ?? "?"),
    fields,
    castChanges: castDiffs(prev, next, sharedSet),
    locationChanged: (prev.shot.location_id ?? "") !== (next.shot.location_id ?? ""),
    prevLocation: prev.location_name || "unset",
    nextLocation: next.location_name || "unset",
  };
}
