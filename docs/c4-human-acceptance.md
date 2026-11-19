# C4 — Human acceptance tracking

Authoritative tracking for the C4 human-acceptance phase. Statuses are
**NOT RUN / PASS / FAIL / BLOCKED**. A check is PASS only when a human
executed it and recorded evidence — nothing here is auto-marked.

- **Build under test:** C3-merged main (`3298ae6`+ closeout `90ad6c2`),
  NSIS `Atomic Dream Engine_0.1.0_x64-setup.exe`, frozen backend.
- **Authoritative checklist:** `docs/n3-human-checklist.md` — the sole
  19-item human audio/editing checklist (A1–A8, B1–B2, C1–C6, D1–D3).
  No additional "completed" human checks are invented.
- **Setup:** see `docs/n3-human-checklist.md` — install the NSIS build,
  run the N3-D driver (`scripts/cdp-n3d-acceptance.cjs`) for the FILM +
  LONG timelines; headphones/speakers required.

## 1. N3 audio/editing checklist — 19 items (all NOT RUN)

| # | Check | Status | Tester | Date | Build | Procedure | Evidence |
|---|-------|--------|--------|------|-------|-----------|----------|
| A1 | Scrub onset immediate | NOT RUN | — | — | — | n3-checklist A1 | — |
| A2 | Scrub continuous | NOT RUN | — | — | — | n3-checklist A2 | — |
| A3 | Scrub stops on release | NOT RUN | — | — | — | n3-checklist A3 | — |
| A4 | Cancel restores | NOT RUN | — | — | — | n3-checklist A4 | — |
| A5 | Silence over gap | NOT RUN | — | — | — | n3-checklist A5 | — |
| A6 | Mute/solo respected | NOT RUN | — | — | — | n3-checklist A6 | — |
| A7 | Automation audible | NOT RUN | — | — | — | n3-checklist A7 | — |
| A8 | Rapid zig-zag | NOT RUN | — | — | — | n3-checklist A8 | — |
| B1 | Preview≈export | NOT RUN | — | — | — | n3-checklist B1 | — |
| B2 | Transport feel | NOT RUN | — | — | — | n3-checklist B2 | — |
| C1 | Edge auto-scroll | NOT RUN | — | — | — | n3-checklist C1 | — |
| C2 | Anchored zoom | NOT RUN | — | — | — | n3-checklist C2 | — |
| C3 | Ruler pinned | NOT RUN | — | — | — | n3-checklist C3 | — |
| C4 | Numeric edits | NOT RUN | — | — | — | n3-checklist C4 | — |
| C5 | Undo predictability | NOT RUN | — | — | — | n3-checklist C5 | — |
| C6 | Keyframe delete | NOT RUN | — | — | — | n3-checklist C6 | — |
| D1 | LONG responsive | NOT RUN | — | — | — | n3-checklist D1 | — |
| D2 | Waveforms render | NOT RUN | — | — | — | n3-checklist D2 | — |
| D3 | Long scrub live | NOT RUN | — | — | — | n3-checklist D3 | — |

## 2. C4.2 — Human audio validation procedures (all NOT RUN)

Each procedure requires a human listener; no automated PASS exists.

| # | Focus | Procedure (build under test) | Status |
|---|-------|------------------------------|--------|
| L1 | Dialogue timing | On the FILM timeline, play the dialogue region start→end; verify each line onsets where the clip sits and no line is clipped early | NOT RUN |
| L2 | Speech-shaped mock-audio disclosure | Open a generated dialogue clip; confirm the UI/inspector labels it simulated (adapter_kind="mock"), and audibly it is speech-shaped test audio — not real TTS | NOT RUN |
| L3 | Music/SFX mixing | Play the full FILM program; verify music ducks under dialogue where automation dips and SFX are audible at their positions | NOT RUN |
| L4 | Gain automation | Scrub and play across an automated region; hear the level move with the curve (n3 A7 equivalent) | NOT RUN |
| L5 | Fades and crossfades | Play each fade-in/out and the xf region; no clicks, pops, or level jumps at edges | NOT RUN |
| L6 | Clipping/limiter | On a hot mix (multiple loud lanes overlapping), listen for harsh digital clipping; the program limiter should prevent overs while staying transparent | NOT RUN |
| L7 | A/V synchronization | Export FILM to mp4 and play end-to-end; lip/hit points and cuts land where the timeline shows them | NOT RUN |

## 3. C4.3 — Wizard usability checklist (all NOT RUN)

Requires representative first-time users; results are recorded, not invented.

| # | Task | Procedure | Status | Findings |
|---|------|-----------|--------|----------|
| U1 | Navigation clarity | Ask a first-time user to create a project through the wizard; note hesitation points | NOT RUN | — |
| U2 | Save and resume | Fill wizard halfway, close the app, relaunch — does state resume sensibly? | NOT RUN | — |
| U3 | Understandable errors | Trigger a recoverable error (e.g. invalid input); can the user explain what happened? | NOT RUN | — |
| U4 | Simulated-generation labels | Ask the user whether the generated media is real or simulated; check they noticed the mock/simulated labeling | NOT RUN | — |
| U5 | Multi-character continuity | Create two characters, assign to scenes, verify the user understands cast assignment | NOT RUN | — |
| U6 | Non-destructive editing | Have the user trim a clip, undo, redo; check they trust the edit is reversible | NOT RUN | — |
| U7 | Export clarity | Have the user export a timeline and find the resulting file | NOT RUN | — |

## 4. C4.5 — Windows human acceptance checklist (all NOT RUN)

Reproducible procedures; automation covers install mechanics (C3 driver)
but the interactive items below need a person.

| # | Item | Procedure | Status | Evidence |
|---|------|-----------|--------|----------|
| W1 | Clean install | On a fresh profile/VM: run the NSIS installer, follow prompts, launch | NOT RUN | — |
| W2 | Installer prompts | Record each NSIS prompt during install; confirm defaults sane, no scary warnings | NOT RUN | — |
| W3 | Upgrade install | Install over an existing install; verify projects/settings preserved | NOT RUN | — |
| W4 | Windows logoff | With the app open + a job queued, log off/on; check app state after | NOT RUN | — |
| W5 | Shutdown behavior | Close the window during an export; relaunch; verify no orphan processes/corrupt project | NOT RUN | — |
| W6 | File dialogs | Exercise every native picker (import, backup target, export view); paths with spaces/unicode | NOT RUN | — |
| W7 | Backup/restore | Human-driven: export backup, delete project, import, verify contents | NOT RUN | — |
| W8 | Export playback | Export a timeline; play the mp4 in Windows Media Player + one other player | NOT RUN | — |
| W9 | Uninstall + reinstall | Uninstall via Settings→Apps, reinstall, confirm data dir handling + first-run flow | NOT RUN | — |
