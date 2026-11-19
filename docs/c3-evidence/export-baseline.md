# C3-E1 — Export scalability baseline (root cause)

## Finding: the "~750 source" limit is a Windows argv-byte limit, not a source-count limit

The single-pass composite emits **one `-i <path>` argument per unique source**
(`build_composite_argv` deduplicates by resolved path; repeated uses fan out
via `split`/`asplit`). Windows `CreateProcess` caps a command line at
**32,767 characters**, so the number of sources that fit is:

```
ceiling ≈ (32767 − argv_base) / (len("-i ") + len(alias_path))
```

Filtergraph text does **not** contribute — it already rides in a job-local
script file via `-/filter_complex` (N3-D). Only the `-i` paths and fixed
flags count.

## Measured ceiling vs alias path length (this machine)

Method: `build_composite_argv` over N sequential 24-frame clips, distinct
paths, then `sum(len(a)+1 for a in argv)` — the exact bytes Windows sees.

| alias path length | typical case | single-pass ceiling |
|---|---|---|
| ~54 chars | short work dir | **~596** unique sources |
| ~84 chars | `...\data\work\{job}\in{i}.mp4` hardlink alias | **~386** |
| ~124 chars | CAS blob path (hardlink fallback) | **~262** |

Sequential-clip measurement on this machine (`in{i}.mp4` aliases in the
job work dir ≈ 75 chars): **crosses 32,767 between 400 and 700 sources —
argv hits 51,141 chars at n=700**. The "~750" in the previous code
comments assumed ~40-char aliases; real absolute alias paths are longer.

## Reproduction

```python
paths = {f"c{i}": f"{work}/in{i}.mp4" for i in range(n)}
argv, _ = tx.build_composite_argv("ffmpeg", plan, paths, canvas, 23,
                                  "out.mp4", script_path="g.txt")
sum(len(a) + 1 for a in argv)   # > 32767 → CreateProcess ERROR_FILENAME_EXCED_RANGE
```

At the boundary FFmpeg never starts — `CreateProcess` itself fails (or
`supervise.run` reports the OS error), so the failure mode is a hard
startup error, not a mid-render crash. Failure diagnostic in the job:
`OSError: [WinError 206] The filename or extension is too long`.

## Resource profile (single-pass, at boundary)

- **argv**: unbounded, scales linearly with unique sources × path length
- **file handles**: one open demuxer per unique source — all opened
  simultaneously at startup (~750 handles is safe on Windows but not
  the binding constraint)
- **memory**: one decoder instance per unique source; the argv wall is
  hit first in practice
- **temp disk**: aliases are hardlinks (0 bytes); filter script O(graph)
- **intermediates**: none — single pass

## Escape: staged submix

`tx.stage_windows` partitions the program into windows of ≤
`STAGE_MAX_INPUTS` (200) unique sources; each window renders to a
lossless FFV1+PCM intermediate; the concat demuxer (a file list, not
argv) joins them into the final encode. Measured: **1500 unique sources
→ 8 stages, max stage argv 3,500 chars** — bounded at any source count.

Verified by `tests/test_c3_staged_export.py`.
