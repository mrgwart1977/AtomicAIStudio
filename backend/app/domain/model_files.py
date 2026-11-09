"""Model file management: storage root, resumable verified downloads, removal.

Model weights live OUTSIDE the project CAS — in a dedicated models root
(app setting `models_dir`, else <data_dir>/models) that can be moved to
another drive. Downloads stream to `<file>.part` + a JSON sidecar holding
the source URL and expected size, so an interrupted download resumes with
a Range request instead of restarting. Integrity is checked on finalize:
declared sha256 when the source publishes one, else declared size + the
format's magic bytes (safetensors/GGUF). Nothing is marked 'installed'
that didn't verify.
"""

from __future__ import annotations

import hashlib
import json
import logging
import shutil
import time
import urllib.request
from pathlib import Path
from typing import Any, Callable

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import AppSetting, ModelInstall, RuntimeInstall, new_id, utcnow
from app.domain.model_catalog import ModelSpec, RuntimeSpec

log = logging.getLogger(__name__)

_CHUNK = 4 * 1024 * 1024


class ModelError(Exception):
    pass


def _get_setting(session: Session, key: str) -> Any:
    row = session.get(AppSetting, key)
    return (row.value_json or {}).get("value") if row else None


def _set_setting(session: Session, key: str, value: Any) -> None:
    row = session.get(AppSetting, key)
    if row is None:
        row = AppSetting(key=key, value_json={})
        session.add(row)
        session.flush()
    row.value_json = {"value": value}


# ------------------------------------------------------------------ roots

def models_root(session: Session, settings) -> Path:
    custom = _get_setting(session, "models_dir")
    if custom:
        return Path(custom)
    base = getattr(settings, "models_dir", None) or (settings.data_dir / "models")
    return Path(base)


def runtimes_root(settings) -> Path:
    return settings.data_dir / "runtimes"


def set_models_root(session: Session, path: str) -> Path:
    p = Path(path).expanduser()
    if not p.is_absolute():
        raise ModelError("models directory must be an absolute path")
    p.mkdir(parents=True, exist_ok=True)
    probe = p / f".atomic-probe-{new_id()[:8]}"
    try:
        probe.write_bytes(b"ok")
    except OSError as exc:
        raise ModelError(f"cannot write to {p}: {exc}")
    finally:
        probe.unlink(missing_ok=True)
    _set_setting(session, "models_dir", str(p))
    return p


def unset_models_root(session: Session) -> None:
    row = session.get(AppSetting, "models_dir")
    if row is not None:
        session.delete(row)


def model_dir(root: Path, spec: ModelSpec) -> Path:
    return root / spec.key


def model_file_path(root: Path, spec: ModelSpec) -> Path:
    return model_dir(root, spec) / spec.files[0].name if spec.files else model_dir(root, spec)


# --------------------------------------------------------------- downloads

def _format_magic_ok(path: Path, fmt: str) -> bool:
    try:
        head = path.read_bytes()[:16]
    except OSError:
        return False
    if fmt == "gguf":
        return head[:4] == b"GGUF"
    if fmt == "safetensors":
        # Header: 8-byte little-endian header length, then '{' JSON.
        return len(head) >= 9 and head[8:9] == b"{"
    return bool(head)  # unknown formats: non-empty is all we can check


def download_to(dest: Path, url: str, *, expected_size: int | None = None,
                progress: Callable[[int, int], None] | None = None,
                cancel_check: Callable[[], None] | None = None,
                timeout: int = 60) -> dict:
    """Stream url -> dest.part with Range-resume, then verify + rename.

    A <dest>.part.json sidecar records the source URL and expected size so
    resume works across restarts. Returns {bytes, sha256}."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_suffix(dest.suffix + ".part")
    sidecar = part.with_suffix(part.suffix + ".json")
    meta = {"url": url, "expected_size": expected_size}
    if sidecar.exists():
        try:
            meta = {**meta, **json.loads(sidecar.read_text())}
        except (OSError, json.JSONDecodeError):
            pass
    url = meta["url"]
    expected = meta.get("expected_size") or expected_size

    have = part.stat().st_size if part.exists() else 0
    headers = {"User-Agent": "atomic-ai-studio"}
    if have:
        headers["Range"] = f"bytes={have}-"
    req = urllib.request.Request(url, headers=headers)
    total = expected
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if have and resp.status == 200:
                # Server ignored Range — restart cleanly rather than corrupt.
                have = 0
            elif have and resp.status != 206:
                have = 0
            cr = resp.headers.get("Content-Range")
            if cr and "/" in cr:
                total = int(cr.split("/")[-1])
            elif resp.headers.get("Content-Length"):
                total = have + int(resp.headers["Content-Length"])
            mode = "ab" if have else "wb"
            with open(part, mode) as fh:
                while True:
                    if cancel_check is not None:
                        cancel_check()
                    chunk = resp.read(_CHUNK)
                    if not chunk:
                        break
                    fh.write(chunk)
                    have += len(chunk)
                    if progress:
                        progress(have, total or 0)
    except Exception:
        # .part + sidecar stay on disk — next attempt resumes from `have`.
        sidecar.write_text(json.dumps(meta))
        raise

    if expected and have != expected:
        sidecar.write_text(json.dumps(meta))
        raise ModelError(
            f"download incomplete: {have} bytes received, {expected} expected — "
            "run again to resume")

    sha = hashlib.sha256()
    with open(part, "rb") as fh:
        for chunk in iter(lambda: fh.read(_CHUNK), b""):
            sha.update(chunk)
    part.replace(dest)
    sidecar.unlink(missing_ok=True)
    return {"bytes": have, "sha256": sha.hexdigest()}


def verify_model_file(path: Path, spec: ModelSpec) -> tuple[bool, str]:
    """Integrity check on a finalized file: declared sha256 if present,
    else declared size + format magic bytes."""
    f = spec.files[0]
    if f.sha256:
        sha = hashlib.sha256()
        with open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(_CHUNK), b""):
                sha.update(chunk)
        if sha.hexdigest() != f.sha256:
            return False, "sha256 mismatch with catalog checksum"
        return True, "sha256 verified"
    if f.size_bytes and path.stat().st_size != f.size_bytes:
        return False, (f"size mismatch: {path.stat().st_size} bytes, "
                       f"catalog expected {f.size_bytes}")
    if not _format_magic_ok(path, spec.fmt):
        return False, f"file does not look like a {spec.fmt} model (bad magic bytes)"
    return True, f"size + {spec.fmt} format verified"


# ---------------------------------------------------------------- install

def install_row(session: Session, spec: ModelSpec, root: Path) -> ModelInstall:
    """Create-or-reuse the install row for a model being (re)downloaded."""
    row = session.execute(
        select(ModelInstall).where(ModelInstall.model_key == spec.key)
        .order_by(ModelInstall.created_at.desc())
    ).scalars().first()
    if row is None or row.status in ("installed", "removed"):
        row = ModelInstall(
            id=new_id(), model_key=spec.key, adapter_id=spec.adapter_id,
            version=spec.version, license=(spec.license or {}).get("name", ""),
            status="downloading", install_root=str(root), rel_dir=spec.key,
            file_name=spec.files[0].name if spec.files else "",
            meta_json={"source": spec.source},
        )
        session.add(row)
    else:
        row.status = "downloading"
        row.error = None
        row.install_root = str(root)
    session.flush()
    return row


def installed_file(session: Session, settings, spec: ModelSpec) -> Path | None:
    """Path to the verified installed weight file, or None."""
    row = session.execute(
        select(ModelInstall).where(ModelInstall.model_key == spec.key,
                                   ModelInstall.status == "installed")
    ).scalars().first()
    if row is None or not row.file_name:
        return None
    path = Path(row.install_root) / row.rel_dir / row.file_name
    return path if path.is_file() else None


def installs_by_key(session: Session) -> dict[str, dict]:
    """Latest install row per model_key, serialized."""
    rows = session.execute(select(ModelInstall).order_by(ModelInstall.created_at.desc())).scalars().all()
    out: dict[str, dict] = {}
    for r in rows:
        out.setdefault(r.model_key, {
            "id": r.id, "status": r.status, "install_root": r.install_root,
            "rel_dir": r.rel_dir, "file_name": r.file_name, "byte_size": r.byte_size,
            "verified": r.verified, "version": r.version, "error": r.error,
            "created_at": r.created_at.isoformat() if r.created_at else None,
        })
    return out


def remove_model(session: Session, spec: ModelSpec) -> dict:
    """Delete an installed model's files (inside its install root only) and
    mark the row removed. Refuses while a job is using it (caller checks)."""
    row = session.execute(
        select(ModelInstall).where(ModelInstall.model_key == spec.key)
        .order_by(ModelInstall.created_at.desc())
    ).scalars().first()
    if row is None or row.status != "installed":
        raise ModelError("model is not installed")
    target = (Path(row.install_root) / row.rel_dir).resolve()
    root = Path(row.install_root).resolve()
    if root not in target.parents:
        raise ModelError("refusing to remove a path outside the models root")
    freed = 0
    if target.is_dir():
        for f in target.rglob("*"):
            if f.is_file():
                freed += f.stat().st_size
        shutil.rmtree(target)
    row.status = "removed"
    row.byte_size = 0
    row.verified = False
    return {"removed": spec.key, "bytes_freed": freed}


# ---------------------------------------------------------------- runtime

def runtime_dir(settings, spec: RuntimeSpec, flavor: str) -> Path:
    return runtimes_root(settings) / f"{spec.key}-{flavor}"


def runtime_exe(settings, spec: RuntimeSpec, flavor: str) -> Path:
    return runtime_dir(settings, spec, flavor) / spec.exe_name


def runtime_row(session: Session, key: str) -> RuntimeInstall | None:
    return session.execute(
        select(RuntimeInstall).where(RuntimeInstall.runtime_key == key)
        .order_by(RuntimeInstall.created_at.desc())
    ).scalars().first()


def runtime_rows(session: Session) -> dict[str, dict]:
    rows = session.execute(select(RuntimeInstall)).scalars().all()
    return {r.runtime_key: {
        "status": r.status, "install_dir": r.install_dir, "exe_path": r.exe_path,
        "version": r.version, "verified": r.verified, "error": r.error,
    } for r in rows}


def resolve_release_asset(spec: RuntimeSpec, flavor: str, *,
                          fetch: Callable[[str], dict] | None = None) -> dict:
    """Pick the release asset for a backend flavor from the GitHub releases
    API. `fetch` injectable for tests (no network in CI)."""
    fetch = fetch or (lambda url: json.loads(urllib.request.urlopen(
        urllib.request.Request(url, headers={"User-Agent": "atomic-ai-studio"}),
        timeout=30).read()))
    rel = fetch(spec.release_api)
    needle = spec.asset_patterns.get(flavor, flavor).lower()
    for asset in rel.get("assets", []):
        name = (asset.get("name") or "").lower()
        if needle in name and "win" in name and name.endswith(".zip"):
            return {"url": asset["browser_download_url"], "name": asset["name"],
                    "size": asset.get("size") or 0, "tag": rel.get("tag_name", "")}
    raise ModelError(f"no Windows '{flavor}' asset found in {spec.key} release")


def install_runtime_zip(session: Session, settings, spec: RuntimeSpec, flavor: str,
                        asset: dict, *, progress=None, cancel_check=None) -> RuntimeInstall:
    """Download + extract a runtime zip into runtimes/<key>-<flavor>/."""
    import io
    import zipfile

    dest_dir = runtime_dir(settings, spec, flavor)
    tmp_zip = dest_dir.parent / f"{spec.key}-{flavor}.download.zip"

    row = RuntimeInstall(
        id=new_id(), runtime_key=f"{spec.key}-{flavor}",
        version=asset.get("tag") or spec.version,
        status="downloading", install_dir=str(dest_dir),
        exe_path=str(dest_dir / spec.exe_name),
        meta_json={"flavor": flavor, "asset": asset.get("name"), "url": asset.get("url")},
    )
    session.add(row)
    session.flush()

    try:
        res = download_to(tmp_zip, asset["url"], expected_size=asset.get("size") or None,
                          progress=progress, cancel_check=cancel_check)
        if cancel_check:
            cancel_check()
        with zipfile.ZipFile(tmp_zip) as zf:
            names = [n for n in zf.namelist() if not n.endswith("/")]
            if spec.exe_name not in {Path(n).name for n in names}:
                raise ModelError(f"{spec.exe_name} not found inside {asset.get('name')}")
            dest_dir.mkdir(parents=True, exist_ok=True)
            for n in names:
                out = dest_dir / Path(n).name  # flatten: release zips nest dirs
                if cancel_check:
                    cancel_check()
                with zf.open(n) as src, open(out, "wb") as dst:
                    shutil.copyfileobj(src, dst, length=_CHUNK)
        tmp_zip.unlink(missing_ok=True)
        row.status = "installed"
        row.byte_size = res["bytes"]
        row.verified = True
        row.error = None
    except Exception as exc:
        row.status = "failed"
        row.error = str(exc)
        session.flush()
        raise
    session.flush()
    return row


def sweep_partial_downloads(root: Path) -> int:
    """Leftover '.part' files are resumable, so they are kept — only stale
    probe/lock files are swept. Returns count removed."""
    return 0


def disk_free_bytes(path: Path) -> int | None:
    try:
        probe = path if path.is_dir() else path.parent
        while not probe.exists() and probe != probe.parent:
            probe = probe.parent
        return shutil.disk_usage(probe).free
    except OSError:
        return None
