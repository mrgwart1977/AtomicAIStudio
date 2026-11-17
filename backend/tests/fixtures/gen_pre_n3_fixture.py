"""Generate a genuine pre-N3 database using the RELEASE code at
663cb5d (post-N2-F, the last code state before N3-A migration
0019_n3a). Run from the repo root with the backend venv python; the
worktree's code takes sys.path priority so `app.*` resolves to the
pre-N3 modules.

Populated coverage: project + character/world/location bibles + scene
+ cast + shots, a 30000/1001 timeline with a video lane and two audio
lanes, clip gain/fades/automation (linear + step), track gain/solo,
one 24f equal-power crossfade, nonzero revision. Each clip's AssetFile
row uses the real sha256 of a synthesized WAV so the installed app's
CAS can be staged with matching bytes (see stage_pre_n3_fixture.ps1 /
the n3d acceptance driver).
"""
import hashlib
import json
import struct
import sys
from fractions import Fraction
from pathlib import Path

WORKTREE = Path(__file__).resolve().parent / "backend"  # ../pre-n3-release/backend
OUT_DB = Path(__file__).resolve().parent / "pre_n3_release.db"
OUT_MEDIA = Path(__file__).resolve().parent / "pre_n3_media"

sys.path.insert(0, str(WORKTREE))
import app  # noqa
assert str(Path(app.__file__).resolve()).startswith(str(WORKTREE)), app.__file__

from app.main import run_migrations
from app.db.session import create_engine_for, session_factory
from app.db.models import (Asset, AssetFile, AssetVersion, Character,
                           EntityRevision, Location, Project, Scene,
                           SceneCharacter, Shot, StorageLocation,
                           Timeline, TimelineClip, TimelineCrossfade,
                           TimelineTrack, World, new_id)


def wav_tone(path: Path, freq: float, seconds: float = 8.0,
             rate: int = 48000, channels: int = 2,
             amp: float = 0.35) -> str:
    """Deterministic sine WAV (16-bit PCM); returns sha256 hex."""
    import math
    n = int(seconds * rate)
    with open(path, "wb") as f:
        f.write(b"RIFF")
        f.write(struct.pack("<I", 36 + n * channels * 2))
        f.write(b"WAVEfmt ")
        f.write(struct.pack("<IHHIIHH", 16, 1, channels, rate,
                            rate * channels * 2, channels * 2, 16))
        f.write(b"data")
        f.write(struct.pack("<I", n * channels * 2))
        for i in range(n):
            v = int(amp * 32767 * math.sin(2 * math.pi * freq * i / rate))
            for _ in range(channels):
                f.write(struct.pack("<h", v))
    return hashlib.sha256(path.read_bytes()).hexdigest()


OUT_DB.unlink(missing_ok=True)
OUT_MEDIA.mkdir(parents=True, exist_ok=True)
url = f"sqlite:///{OUT_DB.as_posix()}"
run_migrations(url)

engine = create_engine_for(url)
sf = session_factory(engine)

WAVS = {}  # name -> (path, sha, rate)
for name, freq, rate, ch in (("dia.wav", 440.0, 48000, 2),
                             ("mus.wav", 330.0, 44100, 2),
                             ("sfx.wav", 660.0, 48000, 2),
                             ("vo.wav", 500.0, 48000, 1)):
    p = OUT_MEDIA / name
    sha = wav_tone(p, freq, rate=rate, channels=ch)
    WAVS[name] = (p, sha, rate)

with sf() as s:
    loc = StorageLocation(id=new_id(), label="Primary",
                          root_path="D:/cas", is_default=True)
    s.add(loc)
    p = Project(id=new_id(), name="Pre-N3 Feature", template="blank")
    s.add(p)
    s.flush()

    ch = Character(id=new_id(), project_id=p.id, name="Ada",
                   summary="Protagonist", revision=3,
                   profile_json={"appearance": "short hair",
                                 "voice": "warm alto"})
    w = World(id=new_id(), project_id=p.id, name="Delta City",
              summary="Neon harbor", revision=2,
              lore_json={"tone": "noir", "era": "2088"})
    s.add_all([ch, w])
    s.flush()
    lc = Location(id=new_id(), project_id=p.id, world_id=w.id,
                  name="Pier 9", summary="Rain-soaked dock",
                  revision=1)
    s.add(lc)
    s.flush()

    sc = Scene(id=new_id(), project_id=p.id, title="Dock Arrival",
               intent="Ada lands at the pier", position=0,
               world_id=w.id, location_id=lc.id, revision=2)
    s.add(sc)
    s.flush()
    s.add(SceneCharacter(id=new_id(), project_id=p.id,
                         scene_id=sc.id, character_id=ch.id,
                         role="lead"))
    shots = []
    for i, (t, pr) in enumerate((("Wide: pier", "wide establishing"),
                                ("CU: Ada", "reaction close-up"),
                                ("Insert: case", "macguffin insert"))):
        sh = Shot(id=new_id(), project_id=p.id, scene_id=sc.id,
                  title=t, prompt=pr, shot_type="medium",
                  duration_sec=3.0, position=i, revision=1)
        s.add(sh)
        shots.append(sh)
    s.flush()

    tl = Timeline(id=new_id(), project_id=p.id, name="Feature Cut",
                  fps_num=30000, fps_den=1001)
    tl.revision = 12
    s.add(tl)
    s.flush()
    v1 = TimelineTrack(id=new_id(), project_id=p.id, timeline_id=tl.id,
                       kind="video", position=0, name="V1")
    a1 = TimelineTrack(id=new_id(), project_id=p.id, timeline_id=tl.id,
                       kind="audio", position=1, name="A1-dialogue",
                       gain_db=-3.0)
    a2 = TimelineTrack(id=new_id(), project_id=p.id, timeline_id=tl.id,
                       kind="audio", position=2, name="A2-music",
                       gain_db=-9.0)
    s.add_all([v1, a1, a2])
    s.flush()

    def mk_asset(name, kind, path, sha, frames, fsn, fsd, dur):
        af = AssetFile(sha256=sha, storage_location_id=loc.id,
                       rel_path=f"blobs/{sha[:2]}/{sha}",
                       byte_size=path.stat().st_size, status="ready")
        s.add(af)
        s.flush()
        a = Asset(id=new_id(), project_id=p.id, name=name, kind=kind,
                  origin="generated", scope="project", status="ready",
                  uri=f"blobs/{sha[:2]}/{sha}", sha256=sha,
                  byte_size=path.stat().st_size)
        s.add(a)
        s.flush()
        av = AssetVersion(id=new_id(), asset_id=a.id, version_no=1,
                          sha256=sha,
                          media_json={"frames": frames,
                                      "fps_num": fsn, "fps_den": fsd,
                                      "duration_s": dur,
                                      "has_audio": True})
        s.add(av)
        s.flush()
        a.current_version_id = av.id
        return a, av

    assets = {}
    for name, kind, rate, ch in (("dia.wav", "audio", 48000, 2),
                                 ("mus.wav", "audio", 44100, 2),
                                 ("sfx.wav", "audio", 48000, 2),
                                 ("vo.wav", "audio", 48000, 1)):
        pth, sha, rate_ = WAVS[name]
        frames = int(8.0 * rate_)
        assets[name] = mk_asset(name, kind, pth, sha, frames,
                                rate_, 1, 8.0)

    def clip(track, asset_key, start, i, o, pos, **kw):
        a, av = assets[asset_key]
        _, sha, rate = WAVS[asset_key]
        frames = int(8.0 * rate)
        c = TimelineClip(
            id=new_id(), project_id=p.id, timeline_id=tl.id,
            track_id=track.id, position=pos, start_frames=start,
            source_asset_id=a.id, source_version_id=av.id,
            source_sha256=sha, in_frame=i, out_frame=o,
            src_frames=frames, src_fps_num=rate, src_fps_den=1,
            src_duration_s=8.0, src_has_audio=True, **kw)
        s.add(c)
        s.flush()
        return c

    # dialogue pair: gain + fades + step automation on d1, linear on d2
    d1 = clip(a1, "dia.wav", 0, 0, 240000, 0,
              gain_db=-3.0, fade_in_units=4800, fade_in_curve="equal_power",
              fade_out_units=2400, fade_out_curve="linear",
              automation_json=json.dumps(
                  [{"id": "p1", "tl_frame": 30, "gain_db": 0.0},
                   {"id": "p2", "tl_frame": 90, "gain_db": -9.0}]),
              automation_interp="step")
    d2 = clip(a1, "dia.wav", 144, 48000, 288000, 1,
              gain_db=0.0,
              automation_json=json.dumps(
                  [{"id": "p3", "tl_frame": 0, "gain_db": -6.0},
                   {"id": "p4", "tl_frame": 60, "gain_db": 0.0}]),
              automation_interp="linear")
    m1 = clip(a2, "mus.wav", 0, 0, 384000, 0,
              gain_db=-6.0, fade_out_units=9600,
              fade_out_curve="equal_power")
    s1 = clip(a2, "sfx.wav", 200, 0, 72000, 1, gain_db=2.0)
    # 24f equal-power crossfade between the two dialogue clips:
    # d1 tail extends, d2 head reaches — window inside both handles.
    s.add(TimelineCrossfade(id=new_id(), project_id=p.id,
                            timeline_id=tl.id, out_clip_id=d1.id,
                            in_clip_id=d2.id, tl_start=96,
                            tl_frames=24, curve="equal_power"))
    # undo history presence — genuine shape: stack entries each backed
    # by an entity_revisions snapshot (what the release's _touch wrote).
    from app.domain.timeline import timeline_snapshot
    snap = timeline_snapshot(s, tl)
    for rev, label in ((11, "clip.place"), (12, "clip.automation")):
        s.add(EntityRevision(
            id=new_id(), project_id=p.id, entity_type="timeline",
            entity_id=tl.id, revision=rev, snapshot_json=snap,
            summary=label))
    tl.undo_stack_json = [{"rev": 11, "label": "clip.place"},
                          {"rev": 12, "label": "clip.automation"}]
    s.commit()

print(f"wrote {OUT_DB} (alembic 0018_n2f head), media -> {OUT_MEDIA}")
for name, (p, sha, rate) in WAVS.items():
    print(f"  {name}: sha {sha[:16]}… rate {rate}")
