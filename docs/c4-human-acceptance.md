# C4 — Human acceptance tracking

Authoritative tracking for the C4 human-acceptance phase. Statuses are
**NOT RUN / PASS / FAIL / BLOCKED**. A check is PASS only when a human
executed it and recorded evidence — nothing here is auto-marked.

## Inventory reconciliation

**42 unique checks.** Breakdown: 19 N3 items (A1–A8, B1–B2, C1–C6,
D1–D3) + 7 listening procedures (L1–L7) + 7 wizard usability tasks
(U1–U7) + 9 Windows acceptance items (W1–W9) = **42** carried-forward
checks. Section 5 adds K1–K5 keyboard/accessibility checks new to C4
(required by the C4 scope — keyboard operability and screen-reader
output cannot be auto-verified) → **47 total unique identifiers**.
An earlier report said "35" — that was an arithmetic error. Every row
has a unique, unambiguous identifier (`grep -oE '^\| [A-Z][0-9]+'` →
47 unique ids, zero duplicates).

**Intentional overlap, not double-counting:** L4 (gain automation)
deliberately mirrors n3 A7's focus from the generated-content side —
they are distinct check rows with distinct procedures and evidence.
A4 (n3) and W5 (Windows shutdown) share surface but different
procedures/evidence. No check is counted twice; no N3 item is
omitted — all 19 are preserved verbatim in section 1.

## Shared preconditions

- **Build under test:** C3-merged main (`3298ae6` + closeout `90ad6c2`
  + PR #162 head) — NSIS `Atomic Dream Engine_0.1.0_x64-setup.exe`
  built from the C4 head commit, frozen backend.
- Install the NSIS build; headphones/speakers required for all
  listening checks. Run the N3-D driver (`scripts/cdp-n3d-acceptance.cjs`)
  once to seed the FILM + LONG timelines.
- Record tester name, date, and the build's commit hash on every row
  that changes from NOT RUN.
- **Defect ref** = link/id of the filed issue or fix commit when a
  check fails or is blocked; "—" while NOT RUN or when PASS with no
  defect.

## 1. N3 audio/editing checklist — 19 items (all NOT RUN)

Check names carried **verbatim** from `docs/n3-human-checklist.md`;
"How" (exact steps) and "Pass if" (expected result) are authoritative
there and are not repeated or reworded here. Purpose = the check name.

| # | Check (verbatim) | Steps / expected | Status | Tester | Date | Build | Evidence | Defect ref |
|---|-------|------------------|--------|--------|------|-------|----------|------------|
| A1 | Scrub onset feels immediate | n3-checklist A1 | NOT RUN | — | — | — | — | — |
| A2 | Scrub is continuous, not stuttery | n3-checklist A2 | NOT RUN | — | — | — | — | — |
| A3 | Scrub stops cleanly on release | n3-checklist A3 | NOT RUN | — | — | — | — | — |
| A4 | Cancel restores | n3-checklist A4 | NOT RUN | — | — | — | — | — |
| A5 | Scrub over silence/gap is silent | n3-checklist A5 | NOT RUN | — | — | — | — | — |
| A6 | Scrub respects mute/solo | n3-checklist A6 | NOT RUN | — | — | — | — | — |
| A7 | Scrub reflects automation | n3-checklist A7 | NOT RUN | — | — | — | — | — |
| A8 | Rapid zig-zag | n3-checklist A8 | NOT RUN | — | — | — | — | — |
| B1 | Preview matches export intent | n3-checklist B1 | NOT RUN | — | — | — | — | — |
| B2 | Play/pause/stop feel | n3-checklist B2 | NOT RUN | — | — | — | — | — |
| C1 | Clip drag has edge auto-scroll | n3-checklist C1 | NOT RUN | — | — | — | — | — |
| C2 | Anchored zoom | n3-checklist C2 | NOT RUN | — | — | — | — | — |
| C3 | Ruler stays visible | n3-checklist C3 | NOT RUN | — | — | — | — | — |
| C4 | Numeric edits don't displace the timeline | n3-checklist C4 | NOT RUN | — | — | — | — | — |
| C5 | Undo is predictable | n3-checklist C5 | NOT RUN | — | — | — | — | — |
| C6 | Keyframe delete | n3-checklist C6 | NOT RUN | — | — | — | — | — |
| D1 | App stays responsive | n3-checklist D1 | NOT RUN | — | — | — | — | — |
| D2 | Waveforms render | n3-checklist D2 | NOT RUN | — | — | — | — | — |
| D3 | Scrub still feels live | n3-checklist D3 | NOT RUN | — | — | — | — | — |

## 2. C4.2 — Human audio validation procedures (all NOT RUN)

Each procedure requires a human listener; no automated PASS exists.
Preconditions: FILM timeline seeded, headphones/speakers.

| # | Purpose | Steps | Expected result | Status | Evidence | Defect ref |
|---|---------|-------|-----------------|--------|----------|------------|
| L1 | Dialogue timing | Play FILM dialogue region start→end | Each line onsets where the clip sits; no line clipped early | NOT RUN | — | — |
| L2 | Speech-shaped mock-audio disclosure | Open a generated dialogue clip; inspect labeling; listen | UI/inspector says simulated (`adapter_kind="mock"`); audio is speech-shaped test audio, not real TTS | NOT RUN | — | — |
| L3 | Music/SFX mixing | Play full FILM program | Music ducks under dialogue at automation dips; SFX audible at positions | NOT RUN | — | — |
| L4 | Gain automation | Scrub + play across an automated region | Level audibly follows the curve (n3 A7 equivalent — distinct procedure/evidence) | NOT RUN | — | — |
| L5 | Fades and crossfades | Play each fade-in/out and xf region | No clicks, pops, or level jumps at edges | NOT RUN | — | — |
| L6 | Clipping/limiter | Play a hot mix (multiple loud lanes) | Limiter prevents overs, stays transparent — no harsh digital clipping | NOT RUN | — | — |
| L7 | A/V synchronization | Export FILM to mp4; play end-to-end | Lip/hit points and cuts land where the timeline shows them | NOT RUN | — | — |

## 3. C4.3 — Wizard usability checklist (all NOT RUN)

Requires representative first-time users; results are recorded, not
invented. Preconditions: fresh install, first-time user, no coaching.

| # | Purpose | Steps | Expected result | Status | Findings | Defect ref |
|---|---------|-------|-----------------|--------|----------|------------|
| U1 | Navigation clarity | Ask user to create a project through the wizard; note hesitations | User completes the flow without being told next steps | NOT RUN | — | — |
| U2 | Save and resume | Fill wizard halfway, close app, relaunch | Wizard state resumes sensibly — no lost work | NOT RUN | — | — |
| U3 | Understandable errors | Trigger a recoverable error (invalid input) | User can explain what happened and what to do | NOT RUN | — | — |
| U4 | Simulated-generation labels | Ask user whether generated media is real or simulated | User noticed mock/simulated labeling | NOT RUN | — | — |
| U5 | Multi-character continuity | Create two characters, assign to scenes | User understands cast assignment | NOT RUN | — | — |
| U6 | Non-destructive editing | Trim a clip, undo, redo | User trusts the edit is reversible | NOT RUN | — | — |
| U7 | Export clarity | Export a timeline; locate the file | User finds the exported file unaided | NOT RUN | — | — |

## 4. C4.5 — Windows human acceptance checklist (all NOT RUN)

Reproducible procedures; automation covers install mechanics (C3 driver)
but the interactive items below need a person. Preconditions: Windows
machine/VM, NSIS installer artifact from the build under test.

| # | Purpose | Steps | Expected result | Status | Evidence | Defect ref |
|---|---------|-------|-----------------|--------|----------|------------|
| W1 | Clean install | Fresh profile/VM: run NSIS installer, follow prompts, launch | App installs and launches to first-run | NOT RUN | — | — |
| W2 | Installer prompts | Record each NSIS prompt during install | Defaults sane; no scary warnings | NOT RUN | — | — |
| W3 | Upgrade install | Install over an existing install | Projects/settings preserved | NOT RUN | — | — |
| W4 | Windows logoff | App open + job queued; log off/on | App state consistent after logon (queued job safe or reported) | NOT RUN | — | — |
| W5 | Shutdown behavior | Close window during an export; relaunch | No orphan processes; project not corrupt | NOT RUN | — | — |
| W6 | File dialogs | Exercise every native picker (import, backup target, export view); try paths with spaces/unicode | All pickers work; non-ASCII paths handled | NOT RUN | — | — |
| W7 | Backup/restore | Export backup, delete project, import | Contents restored correctly | NOT RUN | — | — |
| W8 | Export playback | Export a timeline; play the mp4 in WMP + one other player | Plays correctly in both | NOT RUN | — | — |
| W9 | Uninstall + reinstall | Uninstall via Settings→Apps, reinstall | Data dir handled sanely; first-run flow works | NOT RUN | — | — |

## 5. Keyboard/accessibility checks — added in C4 (all NOT RUN)

Keyboard operability and screen-reader output cannot be
auto-verified. Preconditions: keyboard only (no mouse) for K1–K3;
NVDA or Narrator for K4; the build under test installed.

| # | Purpose | Steps | Expected result | Status | Evidence | Defect ref |
|---|---------|-------|-----------------|--------|----------|------------|
| K1 | Clip selection + inspector by keyboard | Tab to a lane clip; press Enter | Clip selects; inspector opens with trim/move controls | NOT RUN | — | — |
| K2 | Frame-accurate clip ops by keyboard | Focus a clip; ←/→ nudge; Shift+←/→ ±1s; Ctrl+↑/↓ re-lane; `s` split at playhead; Del remove | Each op commits at integer frames; track change lands on a compatible lane | NOT RUN | — | — |
| K3 | Full editing task keyboard-only | Using Tab + clip keys + inspector numeric fields: select a clip, trim in/out, set start frame, adjust fades/gain, undo, redo | Task completes without pointer input; focus never lost in dead space | NOT RUN | — | — |
| K4 | Screen-reader pass | Run NVDA/Narrator through wizard + timeline | Controls announce names/roles/states; errors announce once via role=alert; clips announce kind/lane/timecode | NOT RUN | — | — |
| K5 | Focus visibility + order | Tab through workspace; trigger an error; submit a command | Focus indicator always visible; focus stays predictable after errors/commands | NOT RUN | — | — |

## 6. Defect register

| Ref | Severity | Description | Status |
|-----|----------|-------------|--------|
| — | — | No human-executed failures recorded yet | — |
