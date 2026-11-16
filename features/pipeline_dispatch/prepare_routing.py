"""Transactional admission for additional video processing capacity."""
import json

CLOUD = "cos-cloud"
CLOUD_KINDS = {"fb_prepare", "tt_prepare", "x_preflight", "x_random", "x_repair"}
FAMILIES = {"fb": {"auto", "video"}, "x": {"queue"}, "tt": {"auto", "queue", "intake", "direct_test"}}
RANK = {"gpu": 0, "cpu": 1, "cos": 2}


def backend(caps):
    return next((name for name in RANK if "backend:" + name in caps), "cpu")


def cloud_capability(cap):
    return (cap == "backend:cos" or cap in CLOUD_KINDS or cap in {"prepare:fb", "prepare:x", "prepare:tt"}
            or cap.startswith(("profile:fb-", "profile:tt-", "profile:x-", "catalog:fb:", "catalog:tt:",
                               "catalog:x:", "asset:tt-outro:", "asset:tt-font:")))


def queue_limit(metrics):
    limit = metrics.get("queue_limit")
    return min(64, limit) if metrics.get("queue_ready") is True and type(limit) is int and limit > 0 else 0


def cloud_health(node, stage):
    if stage != "prepare":
        return "prepare_only_node"
    metrics = json.loads(node["metrics_json"])
    if not queue_limit(metrics):
        return "cos_queue_unavailable"
    return ""


def cloud_candidate(row, caps):
    if row["family"] not in FAMILIES.get(row["channel"], ()) or row["grandfathered"] or row["external_intent"]:
        return False
    meta = json.loads(row["source_meta_json"])
    if any(meta.get(key) for key in ("metadata_only", "legacy_inflight", "reconcile_only", "unknown_outcome", "publish_id", "source_attempt_count")):
        return False
    payload = json.loads(row["payload_json"])
    kind = payload.get("kind")
    if kind and (kind not in CLOUD_KINDS or kind not in caps):
        return False
    request = payload.get("request", {})
    profile = request.get("expected_profile") or request.get("profile") or meta.get("preparation_profile")
    return (not profile or "profile:" + profile in caps) and "prepare:" + row["channel"] in caps


def available_higher(conn, node, config, fresh, exclusions):
    priority = RANK[backend(json.loads(node["capabilities_json"]))]
    if not priority:
        return []
    rows = conn.execute("""SELECT n.node_id,n.capabilities_json,
        (SELECT COUNT(*) FROM pipeline_job j WHERE j.node_id=n.node_id AND j.stage='prepare'
             AND j.status='running') AS active,
        (SELECT COUNT(*) FROM pipeline_worker w WHERE w.node_id=n.node_id AND w.stage='prepare'
             AND w.draining=0 AND w.last_seen_at>=?
             AND COALESCE(json_extract(w.observation_json,'$.tool_status'),'ready')<>'unconfigured'
             AND NOT EXISTS (SELECT 1 FROM pipeline_job j WHERE j.node_id=w.node_id
                         AND j.worker_id=w.worker_id AND j.status='running')) AS idle
        FROM pipeline_node n WHERE n.last_seen_at>=? AND n.node_id<>?""", (fresh, fresh, node["node_id"]))
    higher = []
    for peer in rows:
        caps = set(json.loads(peer["capabilities_json"])) - exclusions.get(peer["node_id"], set())
        rank = RANK[backend(caps)]
        target = config["hosts"].get(peer["node_id"], {}).get("prepare", {}).get("target", 0)
        free = min(peer["idle"], target - peer["active"])
        if free > 0 and rank < priority:
            higher.append({"rank": rank, "caps": caps, "free": free})
    return sorted(higher, key=lambda item: item["rank"])


def reserve_higher(higher, row, required):
    """Leave earlier compatible offers for actually idle higher-priority workers."""
    for peer in higher:
        if peer["free"] and required <= peer["caps"] and "prepare:" + row["channel"] in peer["caps"]:
            peer["free"] -= 1
            return True
    return False
