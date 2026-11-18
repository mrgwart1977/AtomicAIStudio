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


def _median_time(fn, runs=3):
    """Median wall-clock over `runs` calls — medians reject the
    contention spikes that made absolute bounds flaky on shared CI
    (A6/A7 load-flake log)."""
    samples = []
    for _ in range(runs):
        t0 = time.perf_counter()
        fn()
        samples.append(time.perf_counter() - t0)
    return sorted(samples)[len(samples) // 2]


def test_edl_v2_serialization_scales(client):
    """Machine-speed-agnostic guard: the property is O(clips+tracks),
    not an absolute latency. Median time at 250 vs 1000 clips must
    scale near-linearly — a quadratic regression quadruples the ratio
    on ANY host speed, while CI contention inflates both sides of the
    ratio together. The absolute caps are hang guards only."""
    pid = make_project(client)["id"]
    a = _video(client, pid)
    t = _tl(client, pid)
    from app.db.models import Timeline
    from app.domain import timeline as tld

    def measure(n_total):
        with client.app.state.session_factory() as s:
            tl = s.get(Timeline, t["id"])
            d1 = _median_time(lambda: tld.edl_json_v2(s, tl))
            d2 = _median_time(lambda: tld.timeline_snapshot(s, tl))
            clips = len(tld.edl_json_v2(s, tl)["clips"])
        assert clips == n_total
        return d1, d2

    _fill(client, pid, t["id"], 250, a)
    s250 = measure(250)
    _fill(client, pid, t["id"], 750, a)
    s1000 = measure(1000)

    for name, lo, hi in (("edl_json_v2", s250[0], s1000[0]),
                         ("timeline_snapshot", s250[1], s1000[1])):
        assert hi < 5.0, f"{name}({1000}) catastrophic: {hi:.3f}s"
        ratio = hi / max(lo, 1e-3)
        assert ratio < 8.0, (
            f"{name} scaled {ratio:.1f}x for 4x clips "
            f"({lo:.3f}s -> {hi:.3f}s) — expected near-linear")


def test_detail_endpoint_1000_clips(client):
    pid = make_project(client)["id"]
    t = _tl(client, pid)
    _fill(client, pid, t["id"], 1000, _video(client, pid))
    t0 = time.perf_counter()
    d = _detail(client, pid, t["id"])
    dt = time.perf_counter() - t0
    assert len(d["clips"]) == 1000
    assert dt < 2.0, f"detail fetch took {dt:.3f}s"
