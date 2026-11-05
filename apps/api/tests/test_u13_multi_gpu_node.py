"""U13: multi-GPU waiting rule, multi-node group selection, benchmark proofs. Written, not run by the author.

Bug re-insertion list: make wait_expired return True always (wait test fails); drop the datacenter_group check in
pick_node_group (unlabelled test fails); skip the peer proof check in store_benchmark (forged proof test fails);
drop the freshness check in pair_ok (stale test fails).
"""

from __future__ import annotations

import datetime as dt
import uuid
from types import SimpleNamespace as NS

from app.core.config import settings
from app.services import multi_node as mn
from app.services import scheduler_v2 as s2

NOW = dt.datetime(2026, 1, 1, 12, 0, tzinfo=dt.timezone.utc)


def _job(gpus=4, nodes=1, age_s=0):
    eff = {"gpu_count": gpus, "node_count": nodes}
    return NS(spec_v2={"effective_resources": eff}, gpu_count=gpus, created_at=NOW - dt.timedelta(seconds=age_s))


def test_gang_job_waits_then_fails():
    j = _job(age_s=10)
    assert s2.is_gang_job(j) and not s2.wait_expired(j, NOW)
    assert s2.wait_expired(_job(age_s=settings.MULTI_GPU_MAX_WAIT_SECONDS + 1), NOW)
    assert s2.wait_expired(NS(created_at=None), NOW)  # unknown creation time fails closed


def test_single_gpu_job_is_not_gang():
    assert not s2.is_gang_job(_job(gpus=1))
    assert not s2.is_gang_job(NS(spec_v2=None, gpu_count=4))  # v1 jobs unchanged


def _m(label, name=None):
    return NS(id=uuid.uuid4(), datacenter_group=label)


def _b(lat=0.5, bw=25_000, age_s=60):
    return NS(latency_ms=lat, bandwidth_mbps=bw, measured_at=NOW - dt.timedelta(seconds=age_s))


def _full(ms, **kw):
    return {(a.id, b.id): _b(**kw) for a in ms for b in ms if a is not b}


def test_group_needs_label_and_passing_pairs():
    ms = [_m("dc1"), _m("dc1"), _m(None)]
    got = s2.pick_node_group(ms, _full(ms), 2, NOW)
    assert got and got[0] == "dc1" and all(m.datacenter_group == "dc1" for m in got[1])
    assert s2.pick_node_group([_m(None), _m(None)], {}, 2, NOW) is None


def test_slow_stale_or_missing_pair_refused():
    ms = [_m("dc1"), _m("dc1")]
    assert s2.pick_node_group(ms, _full(ms, lat=50), 2, NOW) is None
    assert s2.pick_node_group(ms, _full(ms, bw=100), 2, NOW) is None
    assert s2.pick_node_group(ms, _full(ms, age_s=10**7), 2, NOW) is None
    one_way = {(ms[0].id, ms[1].id): _b()}
    assert s2.pick_node_group(ms, one_way, 2, NOW) is None


def test_groups_never_mix():
    ms = [_m("dc1"), _m("dc2")]
    assert s2.pick_node_group(ms, _full(ms), 2, NOW) is None


def test_rendezvous_block():
    r = s2.rendezvous_for("10.0.0.5", 29500, 1, 2, 4)
    assert r["world_size"] == 8 and r["node_rank"] == 1


def test_cluster_addr_and_label_validation():
    assert mn.valid_cluster_addr("10.1.2.3") and not mn.valid_cluster_addr("8.8.8.8")
    assert not mn.valid_cluster_addr("127.0.0.1") and not mn.valid_cluster_addr("host.example")
    assert mn.valid_label("dc-1") and not mn.valid_label("Bad Label") and not mn.valid_label(None)


def test_nonce_and_peer_proof():
    a, b = uuid.uuid4(), uuid.uuid4()
    n = mn.make_nonce(a, b, int(NOW.timestamp()) + 100)
    assert mn.nonce_ok(n, a, b, int(NOW.timestamp()))
    assert not mn.nonce_ok(n, b, a, int(NOW.timestamp()))  # bound to the pair
    assert not mn.nonce_ok(n, a, b, int(NOW.timestamp()) + 200)  # expired
    assert mn.peer_proof("k1", n, a) != mn.peer_proof("k2", n, a)
