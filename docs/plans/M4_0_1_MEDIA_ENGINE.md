# M4.0.1 — Production Media Engine: binary approval proposal

Intermediate milestone between M4.0 and M4.1 (tracking: issue #31).
Replaces the production container-signature-only video check with a real
ffprobe + full-decode validation pipeline. **This document is the binary
approval gate — nothing is downloaded or bundled until the owner
approves one candidate below.**

## 1. Candidates

### A. BtbN/FFmpeg-Builds — `win64-lgpl-shared` (RECOMMENDED)

| Field | Value |
|---|---|
| Source | `github.com/BtbN/FFmpeg-Builds` — the Windows builder ffmpeg.org links to; daily CI autobuilds, public build scripts |
| Exact build | `ffmpeg-n9.0-latest-win64-lgpl-shared-9.0.zip` (release branch `n9.0.2-47-gb204e94e90`, autobuild 2026-11-09) |
| Publisher | BtbN (Timo Rothenpieler — FFmpeg developer; builds produced by repo GitHub Actions) |
| Arch / compat | x86_64; Windows 10 22H2+ (UCRT) — matches our Tauri target |
| Variant | `lgpl-shared`: GPL-only libraries REMOVED upstream (explicitly: no libx264/libx265, no fdk-aac); dynamic libav\*/sw\* DLLs |
| License | LGPL-2.1+ on FFmpeg code; bundled helper libs BSD/zlib-class. No GPL, no nonfree |
| Redistribution obligations | Ship LGPL license texts + copyright notices; provide FFmpeg source pointer (upstream tag + BtbN build scripts are public — we ship a `licenses/ffmpeg/NOTICE` with the exact commit and source URL); "uses FFmpeg under LGPLv2.1" line in app/about + installer; no FFmpeg modifications → no diff needed; shared DLLs satisfy the replaceability expectation |
| Source availability | exact FFmpeg commit named in filename; BtbN build recipes at repo root — reproducible |
| Size | zip **76.5 MB** (76,521,556 B); we ship only `ffmpeg.exe` + `ffprobe.exe` + the shared DLLs they load (drop `ffplay.exe`, docs) ≈ 140–150 MB installed → est. **+30–35 MB** on the installer |
| SHA-256 | `1969cfbc6a3b0bd5f5ce9ed24801846fc05b7501ae4515ee79ab60d07bd29d16` (from release `digest` field; verified again post-download). Release also publishes `checksums.sha256` |
| Integration | subprocess only (no linking) — matches `media_probe`'s existing spawn model; DLLs sit next to the exes, no PATH needed |

Pinning note: the `/download/latest/` URLs float daily. On approval we
pin a dated autobuild tag (e.g. `autobuild-2026-11-09-12-42`, retained
per BtbN's retention policy) and record its sha256 in
`backend/ffmpeg-manifest.json`; the fetch script refuses drift.

### B. BtbN `win64-lgpl` (static) — fallback

Same source/branch/license, statically linked exes. Zip 160.7 MB,
sha256 `7a80b68db3c6098835af6691a70ff3322b42274d586230d44228be26d90063f0`.
Two self-contained exes (~190 MB raw) → est. +40–45 MB installer.
Simpler staging (no DLLs) but larger and loses the natural
LGPL replaceability story. Kept as fallback if shared-DLL loading
proves fragile in the frozen environment.

### C. gyan.dev builds — REJECTED for bundling

Local machine already has `ffmpeg-9.0.2-full_build` installed via winget.
`ffmpeg -version` confirms `--enable-gpl --enable-version3 … --enable-libx264
--enable-libx265` → **GPL binary, not redistributable** inside a
proprietary installer. The same `--enable-gpl` line applies to gyan's
release-essentials build (one shared build script; essentials is a
smaller feature set, not a different license class). **Retained value:**
already installed locally — it stays the dev/test ffmpeg for generating
and validating fixtures (local use is unaffected by GPL).

### D. PyAV wheels — REJECTED (already excluded)

M4.0 audit proved the wheel ships x264/x265 DLLs. `av` stays a dev-only
dependency, excluded from `atomic-backend.spec`. Optional frame-level
dev tooling only.

### E. ffmpeg.org — no option

ffmpeg.org ships source only; its download page points at BtbN and
gyan.dev. Building our own minimal FFmpeg (e) is possible but adds a
build pipeline for marginal size savings — deferred unless the
installer-size estimate above is unacceptable.

## 2. Recommended

**A — BtbN `ffmpeg-n9.0-latest-win64-lgpl-shared-9.0.zip`**, pinned to the
2026-11-09 autobuild tag, sha256-verified. LGPL-clean, officially
endorsed source, smallest compliant payload, dynamic DLLs.

Open question for the future editor: H.264/HEVC **encoding** — the lgpl
variant drops x264/x265 encoders. Decode and validation are unaffected
(native decoders are LGPL). If the editor later needs H.264 encode we'd
revisit (openh264/kvazaar builds or a separate GPL-evaluated path) — not
M4.0.1 scope.

## 3. Media service architecture

New `backend/app/domain/media/` package; `media_probe.py` becomes a thin
compat shim over it (callers unchanged).

```
media/
  binaries.py   resolve ffmpeg/ffprobe: ATOMIC_* env → bundled bin/ next to
                backend exe → PATH → dev gyan via PATH; records resolved path,
                `ffmpeg -version` banner + `-buildconf` for provenance
  engine.py     probe(path) → ffprobe -print_format json -show_format
                -show_streams (-count_packets/frames when counts needed)
                decode_check(path) → ffmpeg -v error -xerror -nostdin
                -i f -map 0:v:0 -f null -  (full decode, error exit on hit)
                validate(path, spec) → structured ValidationResult
  policy.py     ValidationSpec: expected codec set, w/h, fps, exact
                frames?, duration + tolerances (documented: ±max(0.5 s,
                1 frame) container/time-base slop; VFR → nb_read_frames,
                never fps×duration)
  supervise.py  shared spawn helper on proc_guard Job Objects:
                suspended create → assign → resume; kill-on-close;
                timeout; cancellation token → graceful terminate →
                bounded tree kill
```

- Single entry: `engine.validate()` returns `ValidationResult{ok,
  checks[], failures[], probe{…}, ffmpeg{path,version,build}, timings}` —
  serialized into asset `provenance_json` + job row = audit record.
- Every spawn goes through the Job Object supervisor (same contract as
  sd-cli: fail-closed assign, kill-on-close, post-run dead-check).
- Untrusted-input handling: argv lists only (no shell), resolved
  absolute paths, `-nostdin`, bounded timeout (default 120 s), per-job
  RSS ceiling sampled by the watchdog pattern, temp output dirs owned
  per-job and swept.
- Call sites: generation commit boundary (existing `probe_video`
  caller), future editor/export — same interface.

## 4. Validation policy (generated-video gate)

Asset registration requires, in order: exists+nonempty → ffprobe OK →
≥1 video stream → codec/dims/fps within spec → **full ffmpeg decode
exit 0** → frame count vs contract (exact when `--video-frames` set;
`nb_read_frames` for VFR) → duration within tolerance → commit to CAS.
Any failure: job `failed` with structured reason, temp output deleted,
no asset, no retry.

## 5. Test strategy

Fixtures produced by the already-installed gyan ffmpeg (dev tool only):
valid WebM/VP8 + MP4/H.264 tiny clips; empty file; random-junk bytes;
truncated (head -c); flipped mid-stream bytes; audio-only container;
wrong-resolution clip; wrong frame count; timestamp-broken clip.
Process behavior: fake ffmpeg/ffprobe scripts for timeout, non-zero
crash, hang → cancel, kill-tree verification; concurrent validations;
temp cleanup; commit rollback on validation failure (existing job-test
pattern). No real Wan inference.

## 6. Installer integration (post-approval)

1. `scripts/fetch-ffmpeg.ps1` — downloads pinned zip to
   `backend/vendor/ffmpeg/`, verifies sha256, extracts only
   `ffmpeg.exe`, `ffprobe.exe`, required `*.dll`, `LICENSE*` +
   `licenses/ffmpeg/NOTICE` (source pointer + build config dump).
2. `atomic-backend.spec` `datas` → `ffmpeg_bin/` inside the frozen dir;
   `binaries.py` resolves it relative to `sys._MEIPASS`/app dir.
3. NSIS picks it up with the sidecar tree; uninstall removes it;
   installer grows to ~60–65 MB; no system FFmpeg or dev tools required.
4. Vendor dir gitignored; manifest + script committed; CI `package` job
   verifies staged exes respond to `-version`.

## 7. Estimate & risks

- Download 76.5 MB once per dev/build machine; installer +~30–35 MB.
- Risks: floating `latest` URL (mitigated by dated-tag pin + sha256);
  retention of old autobuilds (14 daily + monthly — manifest refresh is
  one line); ffprobe `-count_frames` cost on long clips (bounded —
  generated clips are seconds long; editor imports get a longer
  configurable timeout); VFR edge cases (policy says measured frames,
  not fps×duration).

## Decision requested

Approve candidate A (or B) for download + bundling. On approval:
fetch → media service → policy wiring → tests → sidecar/installer
integration → report for merge approval. M4.1 remains untouched; #28,
#22 stay open.
