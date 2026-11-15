"""N2-B — waveform envelopes: durable asset.waveform jobs build
deterministic min/max bucket levels stored in CAS, served through a
viewport-windowed endpoint; source provenance pinned by sha; unbuilt
stale/foreign assets refuse cleanly."""
import pytest

from tests.conftest import make_project, wait_for_job
from tests.test_c4b_shot_generate import _commit, _preview
from tests.test_c5a_timeline import _video
from tests.test_n2a_audio import _audio, _wav, media_tools  # noqa: F401


def _tone(path, *, rate=48000, channels=2, seconds=0.5):
    """WAV with a real signal — square-wave ±8192, period 128 frames —
    so envelopes carry honest nonzero extrema (unlike silent _wav)."""
    import struct
    n = int(rate * seconds)
    data = b"".join(
        (struct.pack("<h", 8192) * channels) if (i // 64) % 2 == 0
        else (struct.pack("<h", -8192) * channels)
        for i in range(n))
    hdr = struct.pack(
        "<4sI4s4sIHHIIHH4sI", b"RIFF", 36 + len(data), b"WAVE",
        b"fmt ", 16, 1, channels, rate,
        rate * channels * 2, channels * 2, 16, b"data", len(data))
    path.write_bytes(hdr + data)
    return path


def _wave(client, pid, asset_id, expect=200, key=None):
    return _commit(client, pid, "asset.waveform",
                   {"asset_id": asset_id}, expect=expect, key=key)


def _manifest(client, pid, asset_id, expect=200):
    r = client.get(f"/api/projects/{pid}/assets/{asset_id}/waveform")
    assert r.status_code == expect, r.text
    return r.json() if expect == 200 else r


def _slice(client, pid, asset_id, level, s=0, e=None, expect=200):
    q = f"?level={level}&start_bucket={s}"
    if e is not None:
        q += f"&end_bucket={e}"
    r = client.get(
        f"/api/projects/{pid}/assets/{asset_id}/waveform/level{q}")
    assert r.status_code == expect, r.text
    return r.json() if expect == 200 else r


# ---------- command validation ----------

def test_waveform_requires_audio_asset(client):
    pid = make_project(client)["id"]
    vi = _video(client, pid)
    r = _preview(client, pid, "asset.waveform",
                 {"asset_id": vi["id"]}, expect=422)
    assert "audio" in r.json()["detail"].lower()
    _wave(client, pid, vi["id"], expect=422)


def test_waveform_foreign_asset_refused(client):
    pid, other = make_project(client)["id"], make_project(client)["id"]
    au = _audio(client, other)
    _wave(client, pid, au["id"], expect=404)
    _manifest(client, pid, au["id"], expect=404)
    _slice(client, pid, au["id"], 256, expect=404)


def test_waveform_unprobeable_audio_refused(client):
    pid = make_project(client)["id"]
    au = _audio(client, pid, probe_ok=False)
    _wave(client, pid, au["id"], expect=422)


def test_waveform_missing_before_build(client):
    pid = make_project(client)["id"]
    au = _audio(client, pid)
    _manifest(client, pid, au["id"], expect=409)


# ---------- level ladder (pure unit) ----------

def test_level_ladder_bounds_and_ratio():
    from app.domain.media.waveform import level_ladder, BASE_SPB
    lad = level_ladder(48000)          # 1 s @ 48k — 188 fine buckets
    assert lad == [BASE_SPB, BASE_SPB * 4]  # ≥64 → one coarser too
    lad = level_ladder(48_000_000)     # ~17 min @ 48k
    assert lad[0] == BASE_SPB
    assert all(b == a * 4 for a, b in zip(lad, lad[1:]))
    lad = level_ladder(518_400_000)    # 3 h @ 48k — long recording
    from app.domain.media.waveform import MAX_FINEST_BUCKETS
    finest_buckets = -(-518_400_000 // lad[0])
    assert finest_buckets <= MAX_FINEST_BUCKETS
    assert level_ladder(0) == []


# ---------- envelope correctness (real PCM, no ffmpeg) ----------

def test_envelope_exact_minmax(tmp_path):
    import struct
    from app.domain.media.waveform import envelope_level
    pcm = tmp_path / "x.s16le"
    # 1024 stereo frames: L = index, R = -index — deterministic extrema.
    frames = b"".join(struct.pack("<hh", i, -i) for i in range(1024))
    pcm.write_bytes(frames)
    env = envelope_level(pcm, samples=1024, channels=2, spb=256)
    assert env["ok"] and env["buckets"] == 4
    for b in range(4):
        lo, hi = b * 256, b * 256 + 255
        assert env["peaks"][0][b] == [lo, hi]
        assert env["peaks"][1][b] == [-hi, -lo]


def test_envelope_partial_tail_bucket(tmp_path):
    import struct
    from app.domain.media.waveform import envelope_level
    pcm = tmp_path / "x.s16le"
    pcm.write_bytes(b"".join(struct.pack("<hh", i * 10, 0)
                             for i in range(300)))  # 300 < 512
    env = envelope_level(pcm, samples=300, channels=2, spb=256)
    assert env["buckets"] == 2
    assert env["peaks"][0][1] == [2560, 2990]  # only real samples


def test_envelope_cancellation(tmp_path):
    import struct, threading
    from app.domain.media.waveform import envelope_level
    pcm = tmp_path / "x.s16le"
    pcm.write_bytes(struct.pack("<hh", 1, 1) * 5000)
    ev = threading.Event()
    ev.set()
    env = envelope_level(pcm, samples=5000, channels=2, spb=256,
                         cancel=ev)
    assert env["ok"] is False and env["cancelled"] is True


# ---------- end-to-end job on a real WAV ----------

@pytest.mark.usefixtures("media_tools")
def test_waveform_job_builds_manifest_and_slices(client, tmp_path):
    pid = make_project(client)["id"]
    p = _tone(tmp_path / "tone.wav", rate=48000, channels=2,
              seconds=1.0)
    r = _commit(client, pid, "asset.import",
                {"source_path": str(p), "name": "tone.wav",
                 "kind": "audio"})
    job = wait_for_job(client, pid, r["job_id"])
    assert job["status"] == "succeeded", job
    items = client.get(f"/api/projects/{pid}/assets").json()["items"]
    a = next(x for x in items if x["name"] == "tone.wav")

    r = _wave(client, pid, a["id"])
    job = wait_for_job(client, pid, r["job_id"])
    assert job["status"] == "succeeded", job
    assert job["result_json"]["levels"] >= 1

    m = _manifest(client, pid, a["id"])
    assert m["format"] == "ade-wave/1"
    assert m["sample_rate"] == 48000 and m["channels"] == 2
    assert m["samples"] == 48000 and m["built_sha256"]
    assert len(m["levels"]) == 2 and m["levels"][0]["buckets"] == 188

    spb = m["levels"][0]["spb"]
    sl = _slice(client, pid, a["id"], spb, s=0, e=10)
    assert sl["bucket_start"] == 0 and sl["bucket_end"] == 10
    assert len(sl["peaks"]) == 2 and len(sl["peaks"][0]) == 10
    # Square-wave tone — real nonzero extrema, not silence.
    assert any(pk[1] > 0 and pk[0] < 0 for pk in sl["peaks"][0])
    # Slice bounds clamp honestly.
    sl = _slice(client, pid, a["id"], spb, s=180, e=9999)
    assert sl["bucket_end"] == 188 and len(sl["peaks"][0]) == 8
    _slice(client, pid, a["id"], 999999, expect=404)


@pytest.mark.usefixtures("media_tools")
def test_waveform_deterministic_and_skips_rebuild(client, tmp_path):
    pid = make_project(client)["id"]
    p = _wav(tmp_path / "d.wav", rate=44100, channels=1, seconds=0.5)
    r = _commit(client, pid, "asset.import",
                {"source_path": str(p), "name": "d.wav",
                 "kind": "audio"})
    wait_for_job(client, pid, r["job_id"])
    items = client.get(f"/api/projects/{pid}/assets").json()["items"]
    a = next(x for x in items if x["name"] == "d.wav")
    r = _wave(client, pid, a["id"])
    assert wait_for_job(client, pid, r["job_id"])["status"] == "succeeded"
    m1 = _manifest(client, pid, a["id"])
    # Second run: same bytes → "up to date" skip, same level shas.
    r = _wave(client, pid, a["id"], key="wf-again-01")
    job = wait_for_job(client, pid, r["job_id"])
    assert job["status"] == "succeeded"
    assert job["result_json"]["skipped"] == "waveform up to date"
    m2 = _manifest(client, pid, a["id"])
    assert m1 == m2


@pytest.mark.usefixtures("media_tools")
def test_waveform_mono_and_44k_levels(client, tmp_path):
    pid = make_project(client)["id"]
    p = _wav(tmp_path / "m.wav", rate=44100, channels=1, seconds=2.0)
    r = _commit(client, pid, "asset.import",
                {"source_path": str(p), "name": "m.wav",
                 "kind": "audio"})
    wait_for_job(client, pid, r["job_id"])
    items = client.get(f"/api/projects/{pid}/assets").json()["items"]
    a = next(x for x in items if x["name"] == "m.wav")
    r = _wave(client, pid, a["id"])
    assert wait_for_job(client, pid, r["job_id"])["status"] == "succeeded"
    m = _manifest(client, pid, a["id"])
    assert m["channels"] == 1 and m["sample_rate"] == 44100
    assert m["samples"] == 88200
    sl = _slice(client, pid, a["id"], m["levels"][0]["spb"])
    assert len(sl["peaks"]) == 1


@pytest.mark.usefixtures("media_tools")
def test_waveform_rejects_after_version_change(client, tmp_path):
    """A rebuilt manifest predating the current version → 409 stale."""
    pid = make_project(client)["id"]
    p = _wav(tmp_path / "v.wav", rate=48000, channels=2, seconds=0.25)
    r = _commit(client, pid, "asset.import",
                {"source_path": str(p), "name": "v.wav",
                 "kind": "audio"})
    wait_for_job(client, pid, r["job_id"])
    items = client.get(f"/api/projects/{pid}/assets").json()["items"]
    a = next(x for x in items if x["name"] == "v.wav")
    r = _wave(client, pid, a["id"])
    assert wait_for_job(client, pid, r["job_id"])["status"] == "succeeded"
    # Forge a newer version row → manifest's built_sha no longer
    # matches current → endpoint says rebuild, honestly.
    with client.app.state.session_factory() as s:
        from app.db.models import (Asset, AssetFile, AssetVersion,
                                   new_id)
        asset = s.get(Asset, a["id"])
        old_v = s.get(AssetVersion, asset.current_version_id)
        old_media = dict(old_v.media_json or {})
        old_af = s.get(AssetFile, asset.sha256)
        new_sha = "f" * 64
        s.add(AssetFile(sha256=new_sha,
                        storage_location_id=old_af.storage_location_id,
                        rel_path="blobs/ff/fake", byte_size=1,
                        status="ready"))
        s.flush()
        # New version inherits the old media (as relink-style flows
        # could) — the pinned manifest no longer matches the sha.
        v = AssetVersion(id=new_id(), asset_id=asset.id,
                         version_no=99, sha256=new_sha,
                         media_json=old_media)
        s.add(v); s.flush()
        asset.sha256 = new_sha
        asset.current_version_id = v.id
        s.commit()
    r = _manifest(client, pid, a["id"], expect=409)
    assert "predates" in r.json()["detail"]
