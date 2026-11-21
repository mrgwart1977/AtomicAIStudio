"""U-2a — secure user-model import: inspection pipeline.

Reference-only registration of ONE regular file per request. This module
performs path validation, bounded format inspection, and single-handle
SHA-256 hashing. It NEVER writes to the source path, NEVER executes or
deserializes untrusted code paths (no pickle/torch.load/onnx eval), and
treats every malformed file as a structured refusal — a parser failure
is never a registered artifact.

Windows path safety:
  - is_symlink() misses junctions (dir-only reparse) and other reparse
    types; ancestor components are walked individually and, where the
    platform exposes st_file_attributes, FILE_ATTRIBUTE_REPARSE_POINT is
    refused too.
  - resolve(strict=True) resolves but cannot bind the resolved path to
    the handle — the check→open window is narrowed by a single open
    handle + fstat identity, not eliminated. Residual TOCTOU is tracked
    under Issue #178.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
import struct
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# safetensors header cap — same bound the LoRA verifier uses.
MAX_ST_HEADER = 64 * 1024 * 1024
# GGUF metadata bounds.
GGUF_MAX_KV = 4096
GGUF_MAX_KEY = 256
GGUF_MAX_STRING = 1024 * 1024
GGUF_MAX_META_BYTES = 8 * 1024 * 1024
_HASH_CHUNK = 1024 * 1024

# Windows FILE_ATTRIBUTE_REPARSE_POINT — covers junctions and every other
# reparse type is_symlink()/is_junction() can't see.
_FILE_ATTRIBUTE_REPARSE_POINT = 0x400


class ImportRefused(Exception):
    """Structured refusal. `code` is the taxonomy code; `detail` is a
    short sanitized message that MUST NOT embed the source path."""

    def __init__(self, code: str, detail: str):
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


# ---------------------------------------------------------------- paths

def _is_reparse(p: Path, *, is_dir: bool) -> bool:
    """Any reparse point — symlink, junction, or other Windows reparse
    type — is refused. Each check is defensive: st_file_attributes only
    exists on Windows os.stat results."""
    try:
        if p.is_symlink():
            return True
        if is_dir:
            ij = getattr(p, "is_junction", None)
            if callable(ij) and ij():
                return True
        st = p.lstat()
        attrs = getattr(st, "st_file_attributes", 0)
        if attrs & _FILE_ATTRIBUTE_REPARSE_POINT:
            return True
    except FileNotFoundError:
        return False  # missing, not a link — resolve() reports it
    except OSError:
        return True  # can't classify — refuse
    return False


def validate_source_path(path_str: str) -> Path:
    """Absolute-path + reparse + regular-file validation. Raises
    ImportRefused; returns the resolved Path.

    Checked in the route (cheap — no content reads) and re-checked in
    the job before opening (the path may have changed since enqueue).
    """
    if not path_str or not isinstance(path_str, str):
        raise ImportRefused("path_not_absolute", "a source path is required")
    p = Path(path_str).expanduser()
    if not p.is_absolute():
        raise ImportRefused("path_not_absolute",
                            "source path must be absolute")
    # Ancestor walk — a reparse point anywhere up the chain redirects
    # the open; every component dir is checked, then the file itself.
    if _is_reparse(p, is_dir=False):
        raise ImportRefused("path_is_link",
                            "source file is a link or reparse point")
    for ancestor in p.parents:
        if _is_reparse(ancestor, is_dir=True):
            raise ImportRefused("path_is_link",
                                "a parent directory is a link or reparse point")
    try:
        rp = p.resolve(strict=True)
    except (OSError, RuntimeError):
        raise ImportRefused("path_not_found",
                            "source path does not resolve") from None
    if not rp.is_file():
        raise ImportRefused("path_not_file",
                            "source is not a regular file")
    # Post-resolve re-check — the target could itself be a reparse point
    # reached through a path component not covered above; its ancestors
    # are re-walked too (the resolved tree may differ from the lexical
    # one — e.g. case/shortname normalization or device mappings).
    if _is_reparse(rp, is_dir=False):
        raise ImportRefused("path_is_link",
                            "resolved source is a link or reparse point")
    for ancestor in rp.parents:
        if _is_reparse(ancestor, is_dir=True):
            raise ImportRefused(
                "path_is_link",
                "a resolved parent directory is a link or reparse point")
    return rp


# ---------------------------------------------------------------- formats

def sniff_format(head: bytes) -> str:
    """gguf | safetensors | unknown — byte-prefix only."""
    if head[:4] == b"GGUF":
        return "gguf"
    if len(head) >= 9 and head[8:9] == b"{":
        return "safetensors"
    return "unknown"


# safetensors arch/role suggestion table — signature-only, conservative.
_ST_LORA_HINTS = ("lora_down", "lora_up", "lora_a", "lora_b", ".lora.",
                  "hada", "dora_scale")
_ST_ARCH_SIGS = (
    ("flux", ("double_blocks", "img_in", "txt_in", "vector_in")),
    ("sdxl", ("conditioner.", "text_encoder_2.")),
    ("sd1.5", ("model.diffusion_model.", "first_stage_model.",
               "cond_stage_model.")),
)

# safetensors dtype enum — anything outside this set is a valid container
# carrying an unsupported layout (refused, not silently registered).
_ST_DTYPES = frozenset({
    "BOOL", "F4", "F6_E2M3", "F6_E3M2", "F8_E3M4", "F8_E4M3", "F8_E5M2",
    "F8_E8M0", "F16", "BF16", "F32", "F64",
    "I8", "I16", "I32", "I64", "U8", "U16", "U32", "U64",
})


def _suggest_from_keys(keys: set[str]) -> tuple[str, str]:
    """(suggested_arch, suggested_role) from tensor key signatures."""
    lower = {k.lower() for k in keys}
    role = "lora" if any(
        any(h in k for h in _ST_LORA_HINTS) for k in lower) else ""
    hits = [arch for arch, sigs in _ST_ARCH_SIGS
            if any(any(k.startswith(s) or s in k for s in sigs)
                   for k in lower)]
    arch = hits[0] if len(hits) == 1 else "unknown"
    return arch, role


def parse_safetensors(fh, file_size: int) -> dict[str, Any]:
    """Bounded header parse + tensor-offset validation. Never reads the
    tensor body — header is metadata only."""
    fh.seek(0)
    raw = fh.read(8)
    if len(raw) != 8:
        raise ImportRefused("malformed_safetensors",
                            "truncated safetensors header")
    n = int.from_bytes(raw, "little")
    if n <= 0 or n > MAX_ST_HEADER:
        raise ImportRefused("malformed_safetensors",
                            "safetensors header length out of range")
    if 8 + n > file_size:
        raise ImportRefused("malformed_safetensors",
                            "header length exceeds file size")
    try:
        header = json.loads(fh.read(n))
    except (ValueError, RecursionError):
        # ValueError covers JSONDecodeError; RecursionError is a crafted
        # deeply-nested header — malformed, not a raw crash. Neither may
        # surface attacker-controlled metadata in a job error.
        raise ImportRefused("malformed_safetensors",
                            "header is not parseable JSON") from None
    if not isinstance(header, dict):
        raise ImportRefused("malformed_safetensors",
                            "header is not a tensor map")
    limit = file_size - 8 - n
    tensor_keys: set[str] = set()
    meta: dict[str, str] = {}
    for key, entry in header.items():
        if key == "__metadata__":
            if isinstance(entry, dict):
                meta = {str(k): str(v) for k, v in entry.items()}
            continue
        if not isinstance(entry, dict):
            raise ImportRefused("malformed_safetensors",
                                "tensor entry is not an object")
        offs = entry.get("data_offsets")
        shape = entry.get("shape")
        dtype = entry.get("dtype")
        if (not isinstance(offs, (list, tuple)) or len(offs) != 2
                or not all(type(o) is int for o in offs)):
            raise ImportRefused("tensor_offsets_invalid",
                                "tensor entry lacks valid data_offsets")
        begin, end = offs
        if not (0 <= begin <= end <= limit):
            raise ImportRefused("tensor_offsets_invalid",
                                "tensor data offsets exceed the file")
        if (not isinstance(shape, (list, tuple))
                or not all(type(d) is int and d >= 0 for d in shape)):
            raise ImportRefused("malformed_safetensors",
                                "tensor entry has an invalid shape")
        if not isinstance(dtype, str) or not dtype:
            raise ImportRefused("malformed_safetensors",
                                "tensor entry lacks a dtype")
        if dtype not in _ST_DTYPES:
            # Valid container, unsupported layout — distinct refusal.
            raise ImportRefused(
                "unsupported_format",
                f"tensor dtype {dtype!r} is not supported")
        tensor_keys.add(key)
    return {"tensor_count": len(tensor_keys), "tensor_keys": tensor_keys,
            "metadata": meta}


_GGUF_ARCH_MAP = {
    "llama": "llama", "qwen2": "qwen", "qwen3": "qwen",
    "mistral": "mistral", "mixtral": "mixtral", "gemma": "gemma",
    "gemma2": "gemma", "phi3": "phi", "gpt2": "gpt2",
}

# GGUF value types — index is the enum value.
_GGUF_VT_SIZES = {0: 1, 1: 1, 2: 2, 3: 2, 4: 4, 5: 4, 6: 4, 7: 1,
                  10: 8, 11: 8, 12: 8}
_GGUF_VT_STRING = 8
_GGUF_VT_ARRAY = 9


def _gguf_string(fh, file_size: int, read_budget: list[int]) -> bytes:
    raw = fh.read(8)
    if len(raw) != 8:
        raise ImportRefused("malformed_gguf", "truncated string length")
    (ln,) = struct.unpack("<Q", raw)
    if ln > GGUF_MAX_STRING:
        raise ImportRefused("metadata_too_large",
                            "gguf string exceeds bound")
    read_budget[0] += ln
    if read_budget[0] > GGUF_MAX_META_BYTES:
        raise ImportRefused("metadata_too_large",
                            "gguf metadata exceeds bound")
    data = fh.read(ln)
    if len(data) != ln:
        raise ImportRefused("malformed_gguf", "truncated string")
    return data


def parse_gguf(fh, file_size: int) -> dict[str, Any]:
    """Bounded GGUF v1-3 metadata parse. All arithmetic is checked
    against file_size before any read — crafted lengths can never
    drive reads outside the file."""
    fh.seek(0)
    hdr = fh.read(24)
    if len(hdr) != 24 or hdr[:4] != b"GGUF":
        raise ImportRefused("malformed_gguf", "bad gguf header")
    version, n_tensors, n_kv = struct.unpack("<IQQ", hdr[4:24])
    if version < 1 or version > 3:
        raise ImportRefused("malformed_gguf",
                            f"unsupported gguf version {version}")
    if n_kv > GGUF_MAX_KV:
        raise ImportRefused("metadata_too_large", "gguf kv count exceeds bound")
    read_budget = [0]
    kv: dict[str, Any] = {}
    for _ in range(n_kv):
        key = _gguf_string(fh, file_size, read_budget)
        if len(key) > GGUF_MAX_KEY:
            raise ImportRefused("metadata_too_large",
                                "gguf key exceeds bound")
        vt_raw = fh.read(4)
        if len(vt_raw) != 4:
            raise ImportRefused("malformed_gguf", "truncated kv type")
        (vt,) = struct.unpack("<I", vt_raw)
        k = key.decode("utf-8", "replace")
        if vt == _GGUF_VT_STRING:
            v = _gguf_string(fh, file_size, read_budget).decode(
                "utf-8", "replace")
            kv[k] = v
        elif vt == _GGUF_VT_ARRAY:
            et_raw = fh.read(4)
            if len(et_raw) != 4:
                raise ImportRefused("malformed_gguf",
                                    "truncated array header")
            (et,) = struct.unpack("<I", et_raw)
            cnt_raw = fh.read(8)
            if len(cnt_raw) != 8:
                raise ImportRefused("malformed_gguf", "truncated array count")
            (cnt,) = struct.unpack("<Q", cnt_raw)
            if et == _GGUF_VT_STRING:
                for _s in range(min(cnt, 1 << 20)):
                    _gguf_string(fh, file_size, read_budget)
                kv[k] = f"<array:{cnt} strings>"
            else:
                elem = _GGUF_VT_SIZES.get(et)
                if elem is None or cnt > file_size or cnt * elem > file_size:
                    raise ImportRefused("malformed_gguf",
                                        "gguf array bounds invalid")
                read_budget[0] += cnt * elem
                if read_budget[0] > GGUF_MAX_META_BYTES:
                    raise ImportRefused("metadata_too_large",
                                        "gguf metadata exceeds bound")
                skipped = fh.read(cnt * elem)
                if len(skipped) != cnt * elem:
                    raise ImportRefused("malformed_gguf",
                                        "truncated array data")
                kv[k] = f"<array:{cnt}x{et}>"
        else:
            size = _GGUF_VT_SIZES.get(vt)
            if size is None:
                raise ImportRefused("malformed_gguf",
                                    f"unknown gguf value type {vt}")
            data = fh.read(size)
            if len(data) != size:
                raise ImportRefused("malformed_gguf", "truncated scalar")
            fmt = {0: "<B", 1: "<b", 2: "<H", 3: "<h", 4: "<I", 5: "<i",
                   6: "<f", 7: "<?", 10: "<Q", 11: "<q", 12: "<d"}[vt]
            kv[k] = struct.unpack(fmt, data)[0]
    arch_raw = str(kv.get("general.architecture", ""))
    return {"version": version, "n_tensors": n_tensors,
            "architecture_raw": arch_raw,
            "general_name": str(kv.get("general.name", ""))[:200],
            "suggested_arch": _GGUF_ARCH_MAP.get(arch_raw, "unknown"),
            "kv": {k: v for k, v in kv.items() if isinstance(v, str)
                   and len(v) <= 200}}


# ---------------------------------------------------------------- report

@dataclass
class ImportReport:
    fmt: str = "unknown"
    suggested_arch: str = "unknown"
    suggested_role: str = ""
    sha256: str = ""
    byte_size: int = 0
    hashed_at: str = ""
    registration_stat: dict = field(default_factory=dict)
    tensor_count: int = 0
    metadata: dict = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    evidence: dict = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        """Sanitized report — never contains the source path."""
        return {
            "fmt": self.fmt,
            "suggested_arch": self.suggested_arch,
            "suggested_role": self.suggested_role,
            "sha256": self.sha256,
            "byte_size": self.byte_size,
            "hashed_at": self.hashed_at,
            "registration_stat": self.registration_stat,
            "tensor_count": self.tensor_count,
            "metadata": self.metadata,
            "warnings": self.warnings,
            "evidence": self.evidence,
        }


def _stat_identity(st: os.stat_result) -> dict[str, Any]:
    return {"size": st.st_size, "mtime_ns": st.st_mtime_ns,
            "ino": st.st_ino, "dev": st.st_dev}


def inspect_file(path: Path, *,
                 cancel_check: Callable[[], None] | None = None,
                 progress: Callable[[int, int], None] | None = None,
                 clock: Callable[[], str] | None = None) -> ImportReport:
    """Single-handle inspection: fstat → sniff → bounded parse → sha256
    stream → fstat re-check. Raises ImportRefused on any violation;
    callers treat a refusal as a failed job, never a registered row."""
    from app.db.models import utcnow
    when = clock or (lambda: utcnow().isoformat())

    try:
        fh = open(path, "rb")  # noqa: SIM115 — manual try/finally close
        # keeps every read-path OSError mapped to io_error.
    except OSError:
        raise ImportRefused("io_error", "could not open the source file")
    try:
        st1 = os.fstat(fh.fileno())
        if not stat.S_ISREG(st1.st_mode):
            # F3 — the path was a regular file at stat time, but the
            # handle we actually opened must be proven regular too;
            # devices/FIFOs/etc. refuse before any parse or hash.
            raise ImportRefused("path_not_file",
                                "opened source is not a regular file")
        rep = ImportReport(byte_size=st1.st_size, hashed_at=when(),
                           registration_stat=_stat_identity(st1))
        head = fh.read(16)
        rep.fmt = sniff_format(head)
        if rep.fmt == "safetensors":
            parsed = parse_safetensors(fh, st1.st_size)
            rep.tensor_count = parsed["tensor_count"]
            rep.metadata = parsed["metadata"]
            arch, role = _suggest_from_keys(parsed["tensor_keys"])
            rep.suggested_arch, rep.suggested_role = arch, role
            rep.evidence = {"tensor_keys_sample":
                            sorted(parsed["tensor_keys"])[:12]}
        elif rep.fmt == "gguf":
            parsed = parse_gguf(fh, st1.st_size)
            rep.suggested_arch = parsed["suggested_arch"]
            rep.metadata = {"architecture": parsed["architecture_raw"],
                            "name": parsed["general_name"]}
            rep.evidence = {"gguf_version": parsed["version"],
                            "n_tensors": parsed["n_tensors"],
                            "kv": parsed["kv"]}
        else:
            # Contract: unknown/random content gets no registration —
            # only safetensors and GGUF are inspectable in U-2a.
            raise ImportRefused(
                "unsupported_format",
                "only safetensors and gguf files are accepted")
        # Full-content hash over the SAME open handle — a second open
        # would be a different file-identity window.
        fh.seek(0)
        h = hashlib.sha256()
        done = 0
        while True:
            if cancel_check is not None:
                cancel_check()
            chunk = fh.read(_HASH_CHUNK)
            if not chunk:
                break
            h.update(chunk)
            done += len(chunk)
            if progress is not None:
                progress(done, st1.st_size)
        rep.sha256 = h.hexdigest()
        # File-identity re-check: size/mtime/inode/dev must not have
        # moved during inspection — the check→read race narrowed to the
        # handle lifetime (residual TOCTOU under Issue #178).
        st2 = os.fstat(fh.fileno())
        if _stat_identity(st2) != _stat_identity(st1):
            raise ImportRefused("path_changed",
                                "source file changed during inspection")
    except OSError:
        raise ImportRefused(
            "io_error", "could not read the source file") from None
    finally:
        fh.close()
    return rep
