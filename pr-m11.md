# Milestone 1.1 — Storage, Backup & Recovery Improvements

Strengthens the M1 backup architecture before M2: configurable destinations,
live byte-level progress, safe cancellation, crash recovery, and concurrency
protection — all verified in the packaged Windows app.

## A. Configurable backup destinations

- **`GET/PUT /api/settings/backup-dir`** — app-wide default, persisted in the
  new `app_settings` table. `{"dir": null}` resets to the built-in location.
- **`projects.backup_dir`** — per-project override via `project.update_settings`
  (audited, undoable). Resolution: **project → app default → built-in**, never
  a silent fallback — an unreachable configured dir is a surfaced error.
- **Validation probe** (`domain/backup_dest.py`): real tempfile write+fsync,
  `disk_usage` headroom vs. deduped-blob size estimate, drive-letter/UNC
  reachability, and **FAT32 >4 GiB guard** (a 4 GiB+ package physically can't
  land on FAT32 — checked via `psutil` fstype probe).
- Beginner messages: "Drive D: is not connected — reconnect it or pick another
  folder", "Not enough space — need about 48.0 MB, only 12.3 MB free", "This
  drive uses FAT32, which can't hold files over 4.0 GB…".
- **Compatibility**: built-in dir is still the default; pre-M1.1 backups remain
  in history and restore cleanly (v1 manifest format unchanged).

## B. Large-project performance & recovery

- **Live progress**: `ProgressReporter` streams `bytes_done / bytes_total /
  current_file / rate_bps / eta_seconds / elapsed_seconds` into
  `generation_jobs.progress_json`, published over WS and shown in the Backups
  card + Activity panel.
- **Cancellation**: `cancel_requested` column + `threading.Event` checked
  between every 8 MiB chunk. Queued jobs cancel instantly; running jobs cancel
  at the next chunk boundary. `POST /api/jobs/{id}/cancel` → 409 on terminal
  jobs.
- **Atomic + clean**: `.partial` → fsync → `os.replace`; cancel/failure
  unlinks the partial; staging dirs removed in `finally`.
- **Crash recovery**: `backup_runs` rows are created `running` at job start —
  a crash leaves them honestly interrupted (startup reconcile marks failed) —
  and `sweep_backup_destinations` removes `.partial`/`.staging` across the
  built-in dir, app override, every project override, and the data dir.
- **Concurrency**: per-(project, op-family) lock in `JobEngine.enqueue` —
  backup/asset families can't interleave → 409.
- **Restore still creates a new project** — never mutates the live one.

## C. Measured performance

| Metric | Result |
|---|---|
| 400 MiB incompressible blob export | **peak RSS delta 17.0 MiB** (bounded streaming) |
| Packaged export throughput | ~400 MiB/s (compressible), ~33 MiB/s (incompressible, deflate-bound) |
| Progress latency | 0.25 s emit throttle → smooth WS updates |
| Cancel responsiveness | caught mid-run at 117/981 MB → cancelled, no `.partial` left |

## Evidence

- **53/53 backend tests pass** — 20 new `test_m11.py` (destinations,
  persistence, override, bounded streaming, cancel ×3, crash recovery ×2,
  concurrency, insufficient space, corrupt package, FAT32, v1 compat, restore)
  + all M1/M0 regression.
- `tsc -b && vite build` clean.
- **Packaged verify**: NSIS reinstall over the M1 install → real `0002→0003`
  migration on the live DB → custom dir `C:\AtomicTestBackups` set + persisted
  → exports land there → **live cancel mid-export** → cancelled run row +
  clean destination → `GET /backups` shows cancelled/succeeded/interrupted
  history.
- Screenshots in `docs/m1.1-evidence/`: destination card + estimate,
  live progress w/ ETA countdown (two captures), installed app Activity panel
  with succeeded + cancelled jobs.
- `docs/plans/TRACEABILITY.md` extended with M1.1 matrix; `RELEASE_CHECKLIST`
  updated (network-share soak test still open).

## Remaining limitations

- FAT32 guard keys on the *estimate*; a backup that outgrows mid-write (assets
  added concurrently) can still hit the 4 GiB wall — caught as a write error,
  not preflight.
- UNC/network destinations validate reachability but aren't soak-tested on a
  real share (tracked in release checklist #8).
- ZIP64 is enabled for >4 GiB members; whole-package size is bounded by
  destination free space + filesystem.
- Import/restore run inside one job — no mid-import resume (restart = start
  over, safely).

## Next milestone

Awaiting approval: **M2 — multi-character foundation** (Character Library,
cast assignment, Scene/ShotCharacter, storyboard). Not started.
