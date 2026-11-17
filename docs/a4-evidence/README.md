# A4 installed-candidate evidence

- Candidate: working tree = feat/a3-keyframes `72940cd` + uncommitted A4
  motion-clip implementation (migration `0023_a4`).
- Installer: `Atomic Dream Engine_0.1.0_x64-setup.exe` (76.28 MiB) →
  `%LOCALAPPDATA%\Atomic Dream Engine`; frozen sidecar head `0023_a4`.
- Driver: `scripts/cdp-a4-acceptance.cjs` (CDP-sniffed backend creds —
  same plumbing as the A1/A2/A3 drivers).
- Result: `a4-acceptance.txt` — 15/15 PASS.
- ffprobe: installed app's own bundled binary
  (`backend\_internal\ffmpeg\bin\ffprobe.exe`) — real decode + metadata
  verification of the mock MP4 and the exported film.
