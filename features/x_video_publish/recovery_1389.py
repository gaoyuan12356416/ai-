"""One audited zero-write admission for personal task 361; no general retry policy.

The released reservation is immutable. After ordinary account/source preflight,
the original queue is acquired atomically instead; no intermediate reservation
is revived and no second personal task or execution identity is created.
"""
import json

from features.pipeline_dispatch.x_manual_recovery import fingerprint, identity

GRANTS = "x_video_1389_admission"
CONSUMED = "x_video_1389_admission_consumed"
IDENTITY = "fc335f6d50f2f611b9f1f4976ef174867b7b7a50a97486e92ad297edeab8e61f"
FIRST_PLAN = "12fc0765651deea18bb47864d42e098553dc7e9700224b5d21f6b6962e72d6c4"
EXPECTED = {
    "run": "67b4d47ead71842f504159c3a1c9dd6ff67a189a9ba57fc92e1961250effb1a1",
    "task": "bba81783b187516ee6cfa072e41d9d1ce67ecd7f3d8b5d5cb7b67a0ecab9d827",
    "batch": "a6fafb918e7ec51911bcbd6331572c435ee469861970b6c93d027ef6f4f6247f",
    "reservation": "b5e39f7254b8575274a340d2c10f69bdb101cb82c07a4323bd504743a61fcae4",
    "pool_item": "51e1d3902db7d8fe4384dcff35f58e1f69f2377f22d17f467a22fd5bbe63c650",
    "slot": "af106abea4cc9e81122f2bcdf2b9629a84943c52a5a3d4b4f84542da9d1e1b30",
    "first_audit": "ac1aafb61850369e9946acbbe5c054a7e3883d23c842000368229152b2cf9383",
}
QUERIES = {
    "run": "SELECT * FROM x_post_manual_run WHERE id=1389",
    "task": "SELECT * FROM x_video_publish_task WHERE id=361",
    "batch": "SELECT * FROM x_video_publish_batch WHERE id=315",
    "reservation": "SELECT * FROM x_post_manual_material_reservation WHERE manual_run_id=1389 ORDER BY id",
    "pool_item": "SELECT * FROM x_video_publish_pool_item WHERE id=1008",
    "slot": "SELECT * FROM x_video_publish_pool_slot WHERE id=836",
    "first_audit": "SELECT * FROM x_post_manual_resume_audit WHERE run_id=1389",
}


def require(value, code):
    if not value:
        from features.x_posts.service import XPostError
        raise XPostError("x_video_1389_recovery_rejected", code, 409)


def existing(conn, table=GRANTS):
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone():
        return None
    row = conn.execute("SELECT * FROM " + table + " WHERE run_id=1389").fetchone()
    return dict(row) if row else None


def install(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS x_video_1389_admission (
      run_id INTEGER PRIMARY KEY CHECK(run_id=1389),plan_sha256 TEXT NOT NULL UNIQUE,
      evidence_sha256 TEXT NOT NULL,evidence_json TEXT NOT NULL,
      backup_sha256 TEXT NOT NULL,actor TEXT NOT NULL,reason TEXT NOT NULL,
      authorized_at TEXT NOT NULL)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS x_video_1389_admission_consumed (
      run_id INTEGER PRIMARY KEY CHECK(run_id=1389),plan_sha256 TEXT NOT NULL,
      queue_id INTEGER NOT NULL UNIQUE,queue_sha256 TEXT NOT NULL,consumed_at TEXT NOT NULL,
      FOREIGN KEY(run_id) REFERENCES x_video_1389_admission(run_id))""")
    for table in (GRANTS, CONSUMED):
        # INSERT OR REPLACE can otherwise bypass DELETE triggers when SQLite's
        # recursive_triggers is off (the default).
        conn.execute("CREATE TRIGGER IF NOT EXISTS trg_%s_insert BEFORE INSERT ON %s "
            "WHEN EXISTS(SELECT 1 FROM %s WHERE run_id=NEW.run_id) "
            "BEGIN SELECT RAISE(ABORT,'1389 admission history immutable'); END" % (table, table, table))
        for action in ("UPDATE", "DELETE"):
            conn.execute("CREATE TRIGGER IF NOT EXISTS trg_%s_%s BEFORE %s ON %s "
                "BEGIN SELECT RAISE(ABORT,'1389 admission history immutable'); END" %
                (table, action.lower(), action, table))


def collect(conn):
    return {key: [dict(r) for r in conn.execute(sql)] for key, sql in QUERIES.items()}


def validate(conn, *, initial):
    evidence = collect(conn)
    for key, rows in evidence.items():
        require(len(rows) == 1, key + "_missing_or_ambiguous")
        if key != "run" or initial:
            require(fingerprint(rows) == EXPECTED[key], key + "_drift")
    run, task = evidence["run"][0], evidence["task"][0]
    require(fingerprint(identity(run)) == IDENTITY, "frozen_identity_drift")
    require(run["account_ids_json"] == "[43]" and json.loads(run["material_ids_json"]) == ["5188422"]
        and run["expected_count"] == 1 and run["run_date"] == "2026-11-02"
        and run["source_date"] == "2026-11-01" and run["copy_contract_version"] == 2,
        "frozen_scope_invalid")
    require(not any(run[k] for k in ("queued_count", "published_count", "failed_count", "unknown_count")),
        "run_has_execution_outcome")
    if initial:
        require(run["status"] == "failed_preflight"
            and run["error_code"] == "x_post_manual_preflight_failed"
            and run["error_message"] == "read-only MySQL connection failed: OperationalError"
            and run["finished_at"] == "2026-11-03T10:53:21Z", "original_failure_changed")
    else:
        require(run["status"] in ("queued", "running")
            and not any(run[k] for k in ("error_code", "error_message", "finished_at")),
            "admission_attempt_ended")
    reservation = evidence["reservation"][0]
    require(reservation["id"] == 1426 and reservation["state"] == "released"
        and reservation["release_reason"] == "x_video_batch_preflight_failed", "old_reservation_changed")
    require(task["manual_run_id"] == 1389 and task["batch_id"] == 315
        and task["account_id"] == 43 and task["material_id"] == "5188422", "personal_identity_changed")
    first = evidence["first_audit"][0]
    require(first["identity_sha256"] == IDENTITY and first["plan_sha256"] == FIRST_PLAN,
        "first_authorization_changed")
    empty_queries = (
        ("SELECT 1 FROM x_post_queue WHERE manual_run_id=1389 OR material_key='5188422'", "queue_exists"),
        ("SELECT 1 FROM x_post_manual_preflight_failure WHERE manual_run_id=1389", "item_failure_exists"),
        ("SELECT 1 FROM x_post_manual_material_reservation WHERE material_key='5188422' AND state='active'", "material_reserved"),
        ("SELECT 1 FROM x_post_material_pool WHERE material_key='5188422'", "material_in_native_pool"),
        ("SELECT 1 FROM x_video_publish_task WHERE (material_id='5188422' OR batch_id=315) AND id<>361", "other_personal_task"),
        ("SELECT 1 FROM x_video_publish_pool_item WHERE material_id='5188422' AND id<>1008", "other_pool_item"),
        ("SELECT 1 FROM x_video_publish_pool_slot WHERE material_id='5188422' AND id<>836", "other_pool_slot"),
    )
    for sql, code in empty_queries:
        require(conn.execute(sql).fetchone() is None, code)
    account = conn.execute("SELECT status,publish_approved,drama_language FROM x_authorized_account WHERE id=43").fetchone()
    require(account and account["status"] == "active" and account["publish_approved"] == 1
        and account["drama_language"] == task["drama_language"] == "ja", "account_unavailable_or_language_changed")
    from features.x_video_publish.core import PersonalStore, personal_batch_held
    PersonalStore._occupancy(conn, 43, "5188422", own_batch=315)
    require(not personal_batch_held(conn, 315, 43), "personal_batch_held")
    return evidence


def validate_admission(conn, run_id, prepared, failures):
    """Called only under create_manual_plan's existing BEGIN IMMEDIATE."""
    if run_id != 1389:
        return None
    grant = existing(conn)
    if grant is None:
        return None
    require(existing(conn, CONSUMED) is None, "admission_already_consumed")
    saved = json.loads(grant["evidence_json"])
    require(fingerprint(saved) == grant["evidence_sha256"]
        and all(fingerprint(saved[k]) == expected for k, expected in EXPECTED.items()), "admission_evidence_invalid")
    require(len(prepared) == 1 and not failures, "normal_preflight_must_pass")
    validate(conn, initial=False)
    return grant


def consume(conn, grant, timestamp):
    rows = [dict(r) for r in conn.execute("SELECT * FROM x_post_queue WHERE manual_run_id=1389")]
    require(len(rows) == 1 and rows[0]["account_id"] == 43
        and rows[0]["material_key"] == "5188422" and rows[0]["status"] == "queued", "queue_identity_invalid")
    conn.execute("INSERT INTO x_video_1389_admission_consumed VALUES(1389,?,?,?,?)",
        (grant["plan_sha256"], rows[0]["id"], fingerprint(rows[0]), timestamp))
