import { describe, expect, it } from "vitest";
import { compareShots } from "./continuity";
import type { CastMember, StoryboardShot } from "../../api/types";

function castMember(id: string, name: string, over: Partial<CastMember> = {}): CastMember {
  return {
    id: `cm-${id}`, character_id: id, position: 0,
    character: { id, name, status: "active" },
    source: "shot", ...over,
  };
}

function shot(id: string, over: Partial<StoryboardShot["shot"]> = {},
            cast: CastMember[] = [], inherited: CastMember[] = [],
            location_name = ""): StoryboardShot {
  return {
    shot: {
      id, project_id: "p", scene_id: "s", title: id, description: "",
      intent: "", prompt: "", shot_type: "medium", duration_sec: 0,
      position: 0, status: "active", location_id: null,
      thumbnail_asset_id: null, reference_set_id: null,
      source_asset_id: null, continuity_json: {}, revision: 1,
      created_at: "", updated_at: "", ...over,
    },
    location_name, cast, inherited,
  };
}

describe("compareShots", () => {
  it("reports no differences for identical shots", () => {
    const c = castMember("c1", "Ada");
    const a = shot("a", { location_id: "l1" }, [c], [], "Lab");
    const b = shot("b", { location_id: "l1" }, [c], [], "Lab");
    const d = compareShots(a, b);
    expect(d.fields).toHaveLength(0);
    expect(d.castChanges).toHaveLength(0);
    expect(d.locationChanged).toBe(false);
    expect(d.onlyPrev).toHaveLength(0);
    expect(d.onlyNext).toHaveLength(0);
    expect(d.sharedCharacters).toEqual(["Ada"]);
  });

  it("surfaces cast enters/leaves and continuity field diffs", () => {
    const ada = castMember("c1", "Ada");
    const bob = castMember("c2", "Borin");
    const a = shot("a", {
      continuity_json: { lighting: "overcast", wardrobe: "red coat" },
    }, [ada], [], "Lab");
    const b = shot("b", {
      continuity_json: { lighting: "golden hour", wardrobe: "red coat" },
    }, [bob], [], "Lab");
    const d = compareShots(a, b);
    expect(d.onlyPrev).toEqual(["Ada"]);
    expect(d.onlyNext).toEqual(["Borin"]);
    expect(d.sharedCharacters).toHaveLength(0);
    expect(d.fields).toEqual([
      { label: "Lighting", prev: "overcast", next: "golden hour" },
    ]);
  });

  it("diffs wardrobe/expression in shared cast state", () => {
    const a1 = castMember("c1", "Ada", { wardrobe_variant: "armour" });
    const b1 = castMember("c1", "Ada", { wardrobe_variant: "plain", expression: "smiling" });
    const d = compareShots(shot("a", {}, [a1]), shot("b", {}, [b1]));
    expect(d.sharedCharacters).toEqual(["Ada"]);
    expect(d.castChanges).toHaveLength(2);
    expect(d.castChanges[0]).toMatchObject({ name: "Ada" });
  });

  it("counts inherited scene cast as membership", () => {
    const a = shot("a", {}, [], [castMember("c1", "Ada")]);
    const b = shot("b", {}, [], [castMember("c1", "Ada")]);
    const d = compareShots(a, b);
    expect(d.sharedCharacters).toEqual(["Ada"]);
    expect(d.onlyNext).toHaveLength(0);
  });

  it("flags a changed location with names", () => {
    const d = compareShots(
      shot("a", { location_id: "l1" }, [], [], "Lab"),
      shot("b", { location_id: "l2" }, [], [], "Roof"));
    expect(d.locationChanged).toBe(true);
    expect(d.prevLocation).toBe("Lab");
    expect(d.nextLocation).toBe("Roof");
  });
});
