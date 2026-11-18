# A8 — Filmmaking wizard & integrated acceptance

Status: **implemented, in review — not merged.** Final Phase A milestone.

A8 delivers the integrated filmmaking workflow promised by
`docs/phase-a-proposal.md`: a 15-stage guided path from project to a
verified exported film, for beginners **and** advanced users, built
entirely on A1–A7 commands. It adds no parallel project, scene,
character, generation, job, media, timeline, audio, or export system.

## Architecture

| Layer | What | Where |
|---|---|---|
| Derived state | `GET /api/projects/{id}/wizard` computes stage status, warnings, per-entity generation-state labels, plan/job summaries from **live rows** — nothing is a wizard-private copy | `backend/app/domain/wizard.py`, `app/api/routers.py` |
| Progress | `settings_json.wizard` (v2 blob: step/furthest/mode/screenplay/timeline/plan/export ids) via `project.update_settings` — audited, undoable, rides backups; legacy `settings_json.first_film` seeds a v2 state with a resume notice, never rewritten | `apps/desktop/src/features/wizard/wizard.ts` |
| UI | `WizardPage` + 15 stage components; Beginner/Advanced toggle is a view flag — same rows, no data switch; deep links open the real inspectors | `apps/desktop/src/features/wizard/` |
| Honesty | Nine generation states derived per entity: `planned` (no result, job queued/running or never run), `draft` (entity exists, no attempt), `simulated` (mock-adapter result), `generated` (result from a real adapter — unreachable today), `missing` (result asset gone), `failed`, `cancelled`, `stale` (entity edited after job commit), `approved` (reviewed assembly items / pinned results). Mock output is labelled simulated in badges and a persistent banner. | `wizard.py::_gen_state`, `steps.tsx` badges |

Every mutation goes through `preview → commit` with the existing pins
(`expected_keyframe`/`expected_prompt`, `expected_clip`, `expected_dialogue`,
`expected_timeline`/`expected_edl_sha`, assembly fingerprints). Wizard
commits are tagged `origin="wizard"` in the audit trail.

## Generation-state honesty (A8.5)

- Mock images / mock video / speech-shaped tones are **simulated**, never
  "AI-generated content". The wizard banner states this whenever any
  produced asset exists; per-row badges carry the same label.
- Where real inference is unavailable the steps say so and offer the
  simulation path explicitly ("Generate (simulated)").
- Dialogue voices are tone placeholders — the UI never calls them speech.

## Multi-project / multi-character (A8.6)

Covered by `backend/tests/test_a8_wizard.py`: two fully populated
projects derive independent wizard states; cross-project keyframe/motion/
dialogue rows never appear in the wrong project's derivation; scene cast
with multiple characters + follow targets validates through `shot.update`
planning (same validation as the storyboard page).

## Persistence & recovery (A8.7)

- Wizard progress survives restart (it is project settings JSON) and
  backup/restore — `backup.py` package-import now merges packaged
  `settings_json` into the import stub (pre-A8 it was silently dropped).
- Stored ids are re-validated on load; deleted screenplays/timelines/
  plans drop with user-visible notices, never silently trusted.
- Stale revisions: all generation commits pin resolved state; a stale
  pin refuses with the normal 409 → friendly error surface.
- Failed/cancelled jobs surface per-row as `failed`/`cancelled` and the
  entity returns to its pre-attempt state — re-runnable.
- Undo/redo: wizard commits are ordinary commands; undo works.

## Installed Windows acceptance (A8.13)

Driver: `scripts/a8-acceptance.ps1` (dev-token HTTP against the installed
frozen backend — same pattern as A6/A7).

- **Scenario A — clean install:** see `docs/a8-evidence/acceptance-a.md`.
- **Scenario B — upgrade over populated install:** `docs/a8-evidence/acceptance-b.md`.

## Automated results

| Suite | Result |
|---|---|
| `backend/tests/test_a8_wizard.py` | 16/16 PASS |
| Full backend pytest | see evidence |
| Frontend vitest (`features/wizard` + all) | 413 total, 23 wizard-specific |

## Human acceptance (A8.14)

The spec asks for "the existing 24 human audio/editing checks". The only
human audio/editing checklist is `docs/n3-human-checklist.md` with **19**
enumerated checks (A1–A8, B1–B2, C1–C6, D1–D3) — the "24" figure traces
to a loose reference in `n3-completion-report.md`. The real 19 are
carried forward; none invented.

| Group | Checks | Status |
|---|---|---|
| A — scrubbing feel | A1–A8 | NOT RUN |
| B — preview parity | B1–B2 | NOT RUN |
| C — editing feel | C1–C6 | NOT RUN |
| D — long-program responsiveness | D1–D3 | NOT RUN |

Human PASS/FAIL columns stay empty until a person runs the checklist —
automated results are never promoted into them.

## Known limitations

- Wizard generation (keyframe/motion/dialogue) is **mock-adapter
  simulated** — labeled, deterministic, not creative output.
- Screenplay import supports **one** plain-text paste format (headings
  `INT.`/`EXT.`/`#`, `NAME: line` dialogue, `[bracket]` notes). No
  Fountain/FDX/PDF claims.
- Beginner mode intentionally omits some advanced surfaces (model
  catalog picks, GPU admission, cloud planning) — they are one tab away
  in Advanced mode / workspace inspectors.
- The legacy `first_film` step blob maps to the *nearest* A8 stage —
  resume position is approximate by design and disclosed with a notice.

## Security & release blockers

Unchanged from A7 — `live_gate.dispatch_admission` untouched; issue
**#96** still gates live artifact retrieval; Stage B2b ON HOLD;
credential isolation, provider approval, budget controls, DNS/SSRF,
redirect validation, audit, idempotency all preserved. A8 adds no new
network path — `GET /wizard` is read-only derivation.

Tracked release blockers: see `docs/RELEASE_CHECKLIST.md` (#5–#15,
#22–#27, #96). A8 is **not** a public-release claim.
