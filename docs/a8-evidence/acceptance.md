# A8 installed Windows acceptance — evidence

- **Tested commit:** `4e6fda3` (feat/a8-wizard; final reviewed head —
  rerun after the `backup.py` import-sanitizer hardening; an earlier
  34/34 run covered `c1f4a8c`)
- **Installer:** `apps/desktop/src-tauri/target/release/bundle/nsis/
  Atomic Dream Engine_0.1.0_x64-setup.exe` — 76.43 MiB (NSIS, silent
  `//S` install to `%LOCALAPPDATA%\Atomic Dream Engine\`)
- **Installed exe:** `%LOCALAPPDATA%\Atomic Dream Engine\
  atomic-dream-engine.exe` (10,074,624 bytes)
- **Backend:** PyInstaller-frozen (`backend/dist/atomic-backend`),
  Python 3.14.7 — no system Python required
- **Bundled ffprobe used for validation:** `%LOCALAPPDATA%\Atomic Dream
  Engine\backend\_internal\ffmpeg\bin\ffprobe.exe`
- **Environment:** Windows 11 (10.0.26300), x64; driver
  `scripts/cdp-a8-acceptance.cjs` (CDP port 9232, token sniffed from
  WebView2 — no credentials stored)
- **Run log:** `acceptance.txt` — **34/34 PASS** (22 Scenario A +
  12 Scenario B)
- **Artifacts:** `export-probe.json` (ffprobe JSON of the A18 export).
  The probed .mp4s and `a8-backup.atomicpkg` were produced in this
  directory during the run but are not committed (binary build
  artifacts — regenerate by re-running the driver).

## Scenario A — clean install (22/22)

Full wizard pipeline against a fresh project: install → launch → create
project → film settings (16:9 1280×720 @24/1) → screenplay with 2 scenes
→ 2 characters → 3 dialogue lines → scaffold → planning on every shot
(follow_behind target) → 8 keyframe drafts → mock image generation
(all labeled simulated) → mock video per shot → mock dialogue →
assembly preview → review+apply → timeline trim edit → audio lane gain
→ export 720p → bundled-ffprobe validation (h264, nonzero duration,
audio stream) → restart persistence → backup/restore (wizard progress
survives) → clean shutdown.

## Scenario B — upgrade over populated install (12/12)

The A project stands in as the pre-existing populated installation:
relaunch under the same build → project opens → migration head serves
`/wizard` (15 stages derived) → scenes/shots/keyframes/clips/dialogue/
assets intact → wizard derives ≥8 done stages + honesty all-simulated
→ wizard progress resumed → Beginner↔Advanced switch changes only the
view flag (keyframe revision map byte-identical) → manual edits intact
(tail trim + planning) → timeline/audio lanes healthy → re-export with
ffprobe validation → second restart persistence → clean shutdown.

## Notes

- All generation observed end-to-end is **mock-adapter simulated** —
  labeled `simulated` in every `/wizard` read; honesty banner data
  (`all_simulated: true`) verified at A11 and B5.
- A8 adds no migration — wizard progress rides `settings_json` (merge
  semantics of `project.update_settings`); B2 asserts the existing
  schema carries the new surface.
- The human checklist (`docs/n3-human-checklist.md`, 19 checks) is
  **NOT RUN** — see `docs/a8-acceptance.md` §Human acceptance.
