# A5 acceptance evidence

`a5-acceptance.txt` — `scripts/cdp-a5-acceptance.cjs` run against the
installed `Atomic Dream Engine_0.1.0_x64-setup.exe` (76.33 MiB) built
from the PR #149 head `3b8ba7d` on `feat/a5-dialogue-audio`.

15/15 steps pass, including:

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
- restart persistence; clean shutdown, no orphan backend
