# C4 human acceptance — operator guide

Whoever you are: this is everything you need to run the five test
sessions on the installed Windows app. You do **not** need any cloud
accounts, API keys, or provider access — the build is simulated-only;
any screen implying real AI inference is itself a defect.

The authoritative tracker is `docs/c4-human-acceptance.md` — 47 checks
(A1–A8, B1–B2, C1–C6, D1–D3, L1–L7, U1–U7, W1–W9, K1–K5). Record all
results there.

## 1. Get and verify the installer

File: **`Atomic Dream Engine_0.1.0_x64-setup.exe`** (~76.4 MiB).

It was built from commit `bc8745e` (executable-identical to merged
main `c201623` — only doc files differ). To verify the copy you have:

```
certutil -hashfile "Atomic Dream Engine_0.1.0_x64-setup.exe" SHA256
```

Expected: `a3283915c0032b179fc17b95f4c46219c1a2f18d91c115b20b1ee28e491f5cbf`

If the hash differs and you received the file from engineering, ask
which build it is — record that build's hash/commit as your "Build"
value. Fresh CI artifacts come from the `package` job on main.

## 2. Environment

- Windows 10/11 x64 — a VM or a secondary user profile is ideal
  (W-series includes install/uninstall/logoff).
- Headphones or speakers.
- ~2 GB free disk. Close heavy apps before timing checks.
- Keyboard required throughout; **mouse forbidden in Session 5**.
- Optional for K4: NVDA (nvaccess.org, free) or built-in Narrator.

## 3. Install + first launch

1. Run the NSIS installer, accept defaults, launch the app.
   → This is also check **W1/W2** — take notes while you do it.
2. The app lands at `%LOCALAPPDATA%\Atomic Dream Engine`.
   Project data lives at `%LOCALAPPDATA%\AtomicAIStudio`
   (legacy folder name — expected, not a bug).

## 4. Seed the test project

Sessions 1–2 need the prebuilt **FILM** + **LONG** timelines
(dialogue/music/SFX, fades, automation, 240+ clips). Two ways:

**A — engineering seeds it (easiest):** ask for one run of
`node scripts/cdp-n3d-acceptance.cjs 9227 out\` against your install.
It creates a project named `N3D-<timestamp>` containing both
timelines; you'll see it on the Projects page.

**B — you run it:** needs Node.js + a repo checkout. From the repo
root with the app closed:

```
node scripts\cdp-n3d-acceptance.cjs 9227 out\
```

It launches the installed app itself and exits when done.
Either way, no credentials or network access are involved.

## 5. Run the sessions

Do them in order; each is independent — see
`docs/c4-human-acceptance.md` for per-check steps + expected results.

| Session | Checks | Needs | ~Time |
|---------|--------|-------|-------|
| 1 — N3 audio/editing | A1–D3 (19) | seeded timelines, headphones | 60 min |
| 2 — Listening | L1–L7 | FILM timeline, headphones | 30 min |
| 3 — Usability | U1–U7 | first-time user if possible, fresh project | 45 min |
| 4 — Windows | W1–W9 | logoff rights, second media player | 45 min |
| 5 — Keyboard/a11y | K1–K5 | no mouse; NVDA/Narrator for K4 | 30 min |

Notes:
- **S1:** these are feel judgments — trust your senses; hesitation is
  a finding worth writing down even on a PASS.
- **S3:** if no novice exists, run cold self-test and mark evidence
  "self-test" — lower confidence, still recorded.
- **S5 keyboard map:** Tab moves between controls; Enter/Space selects
  a clip → inspector; ←/→ nudge 1 frame (Shift = ±1 s); Ctrl+↑/↓
  moves a clip between lanes; `s` splits at playhead; Delete removes;
  Ctrl+Z / Ctrl+Shift+Z undo/redo; ruler = slider (arrows/Home/End/
  PgUp/PgDn).

## 6. Record results

Edit `docs/c4-human-acceptance.md` — for every check you run:

- **Status:** `PASS`, `FAIL`, or `BLOCKED` (never delete `NOT RUN`
  rows you didn't execute).
- **Tester** = your name. **Date** = today. **Build** = the commit
  hash of the installed build.
- **Evidence** = filename(s) you captured.
- **Defect ref** = issue number/link if it failed; `—` on clean PASS.

## 7. Evidence — where files go

`docs/c4-evidence/human/` — name files `<ID>-<what>.<ext>`:

- Screenshots: `Win+Shift+S` → save `A3-scrub-tail.png`
- Recordings: `Win+Alt+R` → `D1-long-export.mp4`
- Listening notes: plain text in the Evidence column, e.g.
  `"heard SFX03 ~0.5 s late"` — timestamps make them strong.
- Exported clips keep their paths in `out\` or your chosen export dir.

## 8. Defects

For each FAIL: file a GitHub issue, link it in `Defect ref`:

```
Check: <ID>   Build: <hash>   Severity: critical|high|medium|low
Steps: <numbered, minimal>
Expected: <one line>   Actual: <one line>
Evidence: <file path(s)>
```

Severity: *critical* = crash/data loss/security/wrong project data;
*high* = required action unreachable or wrong result; *medium* =
degraded but workable; *low* = cosmetic.

## 9. Resuming

Everything persists: the tracker doc holds status; the app holds your
projects. To resume mid-session: reopen the app, open the same
project, continue from the next `NOT RUN` row. If the app was
uninstalled mid-testing, note that in the row and re-seed (§4).

## 10. Submit

When every row is PASS/FAIL/BLOCKED:

1. Commit `docs/c4-human-acceptance.md` + `docs/c4-evidence/human/*`,
   or hand the filled tracker + evidence folder to engineering.
2. List any open defects and retests done.
3. C4 closes only on the owner's explicit approval — the review
   decision is not yours to make and not implied by PASS counts.
