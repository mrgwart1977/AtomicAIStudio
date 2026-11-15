"""N1-E — large-timeline performance guards.

Ceilings are deliberately generous (this runs on shared CI) — the
goal is catching asymptotic regressions, not absolute latency. Real
machine numbers live in docs/n1e-acceptance-evidence.md via
scripts/n1e-bench.cjs."""
import time

from tests.conftest import make_project
from tests.test_c5a_timeline import _video, _tl, _detail
from tests.test_c4b_shot_generate import _commit


def _fill(client, pid, tid, n, asset):
    """Insert n clips in-session — O(n) command commits would be too
    slow for a unit test. Rows point at one real asset (FK rules)."""
    from app.db.models import Timeline, TimelineClip, new_id
    from app.domain import timeline as tld
    with client.app.state.session_factory() as s:
        v1 = tld.first_video_track(s, tid)
        pos = 0
        for i in range(n):
            s.add(TimelineClip(
                id=new_id(), project_id=pid, timeline_id=tid,
                track_id=v1.id, position=i, start_frames=pos,
                in_frame=0, out_frame=72,
                source_asset_id=asset["id"],
                source_version_id=asset["version_id"],
                source_sha256=asset["sha256"], src_frames=72,
                src_fps_num=24, src_fps_den=1, src_duration_s=3.0))
            pos += 72
        s.commit()
    return n


def test_edl_v2_serialization_scales(client):
    pid = make_project(client)["id"]
    t = _tl(client, pid)
    a = _video(client, pid)
    _fill(client, pid, t["id"], 1000, a)
    from app.db.models import Timeline
    from app.domain import timeline as tld
    with client.app.state.session_factory() as s:
        tl = s.get(Timeline, t["id"])
        t0 = time.perf_counter()
        edl = tld.edl_json_v2(s, tl)
        d1 = time.perf_counter() - t0
        t0 = time.perf_counter()
        tld.timeline_snapshot(s, tl)
        d2 = time.perf_counter() - t0
    assert len(edl["clips"]) == 1000
    # ~1000-row serialization + snapshot must stay well under a second
    # on CI hardware; the N1-A baseline was ~O(clips + tracks).
    assert d1 < 1.0, f"edl_json_v2 took {d1:.3f}s"
    assert d2 < 1.0, f"timeline_snapshot took {d2:.3f}s"


def test_detail_endpoint_1000_clips(client):
    pid = make_project(client)["id"]
    t = _tl(client, pid)
    _fill(client, pid, t["id"], 1000, _video(client, pid))
    t0 = time.perf_counter()
    d = _detail(client, pid, t["id"])
    dt = time.perf_counter() - t0
    assert len(d["clips"]) == 1000
    assert dt < 2.0, f"detail fetch took {dt:.3f}s"
