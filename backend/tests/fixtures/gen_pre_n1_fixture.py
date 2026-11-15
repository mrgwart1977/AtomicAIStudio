"""Generate a genuine pre-N1 database using the RELEASE code at
149690a (checked out in ../pre-n1-release). Run from the repo root
with the backend venv python; the worktree's code takes sys.path
priority so `app.*` resolves to pre-N1 modules."""
import sys
from pathlib import Path

WORKTREE = Path(__file__).resolve().parent.parent / "pre-n1-release" / "backend"  # sibling of AtomicAIStudio/
OUT = Path(__file__).resolve().parent / "backend" / "tests" / "fixtures" / "pre_n1_release.db"

sys.path.insert(0, str(WORKTREE))
import app  # noqa
assert str(Path(app.__file__).resolve()).startswith(str(WORKTREE)), app.__file__

from app.main import run_migrations
from app.db.session import create_engine_for, session_factory
from app.db.models import (Asset, AssetFile, AssetVersion, Project,
                           StorageLocation, Timeline, TimelineClip,
                           new_id)

OUT.parent.mkdir(parents=True, exist_ok=True)
if OUT.exists():
    OUT.unlink()
url = f"sqlite:///{OUT.as_posix()}"
run_migrations(url)

engine = create_engine_for(url)
sf = session_factory(engine)
with sf() as s:
    loc = StorageLocation(id=new_id(), label="Primary",
                          root_path="D:/cas", is_default=True)
    s.add(loc)
    p = Project(id=new_id(), name="Pre-N1 Film", template="blank")
    s.add(p)
    s.flush()
    tl = Timeline(id=new_id(), project_id=p.id, name="Launch Cut",
                  fps_num=24, fps_den=1)
    tl.revision = 2
    s.add(tl)
    s.flush()
    # two sequential clips, mixed source rates — the interesting
    # conversion case (48f@24/1 then 60f@30000/1001)
    for i, (frames, fsn, fsd) in enumerate(((48, 24, 1), (60, 30000, 1001))):
        sha = ("ab" if i == 0 else "cd") * 32
        s.add(AssetFile(sha256=sha, storage_location_id=loc.id,
                        rel_path=f"blobs/{sha[:2]}/clip{i}",
                        byte_size=100, status="ready"))
        s.flush()
        a = Asset(id=new_id(), project_id=p.id, name=f"clip{i}.mp4",
                  kind="video", origin="generated", scope="project",
                  status="ready", uri=f"blobs/{sha[:2]}/clip{i}",
                  sha256=sha, byte_size=100)
        s.add(a)
        s.flush()
        v = AssetVersion(id=new_id(), asset_id=a.id, version_no=1,
                         sha256=a.sha256,
                         media_json={"probe": {"ok": True,
                                               "duration_s": frames / (fsn / fsd),
                                               "fps": fsn / fsd,
                                               "frames": frames,
                                               "width": 640,
                                               "height": 360,
                                               "codec": "h264"}})
        s.add(v)
        s.flush()
        a.current_version_id = v.id
        s.add(TimelineClip(
            id=new_id(), project_id=p.id, timeline_id=tl.id,
            position=i, source_asset_id=a.id, source_version_id=v.id,
            source_sha256=v.sha256, in_frame=0, out_frame=frames,
            src_frames=frames, src_fps_num=fsn, src_fps_den=fsd,
            src_duration_s=frames / (fsn / fsd)))
    s.commit()

import sqlite3
con = sqlite3.connect(str(OUT))
print("version:", con.execute("select version_num from alembic_version").fetchone())
print("clip cols:", [c[1] for c in con.execute("PRAGMA table_info(timeline_clips)")])
print("clips:", con.execute("select count(*) from timeline_clips").fetchone())
con.close()
print("wrote", OUT)
