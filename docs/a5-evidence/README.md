# A5 acceptance evidence

`a5-acceptance.txt` — `scripts/cdp-a5-acceptance.cjs` run against the
installed `Atomic Dream Engine_0.1.0_x64-setup.exe` (76.34 MiB) built
from the PR #149 reviewed head `bec275b` on `feat/a5-dialogue-audio`.

17/17 steps pass, including:

- populated A4 project (screenplay scene + scaffolded shot)
- two dialogue segments with scene/element/shot/speaker refs
- multiple speakers (Anna + Ben)
- voice-profile inspection — Anna resolves `style: warm`; Ben flags
  `missing`
- planned rational timing (24f @24fps)
- deterministic `mock.audio` generation — hold barrier → release →
  CAS result via audited `dialogue.result`
- deterministic cancel → `cancelled`, no result, 409 re-cancel;
  retry lands a result
- bundled-ffprobe verification of the produced WAV:
  `pcm_s16le`, 48 kHz, ~2.32 s
- audio preview — served bytes are the real RIFF/WAVE
- `dialogue.place` on a DX audio lane (0f + 96f)
- dialogue + user-imported music bed mixed on separate lanes
- `timeline.export` — decoded PCM audio stream in the exported film
- stale rejection — a mid-flight text edit refuses the held job's
  publish; `result_stale` flags the drifted audio; regenerating
  against the new text clears the flag and attaches fresh
- backup/restore — segments, remapped refs, and result assets
  round-trip into a new project (backup v6)
- restart persistence — segments, results, placement, and the stale
  flag survive relaunch; clean shutdown, no orphan backend
