"""Offline FB attribution, identity immutability and source safety tests."""
import copy
import hashlib
import json
import os
import sqlite3
import stat
import tempfile
import unittest
from contextlib import closing, contextmanager
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlencode

from features.social_analytics import facebook as fb


NOW = "2026-10-20T09:00:00Z"
LATER = "2026-10-20T09:01:00Z"
CREATED = "2026-10-19T01:00:00Z"
PUBLISHED = "2026-10-19T03:00:00Z"
NEW_ID = "a" * 32


@contextmanager
def database(path):
    with closing(sqlite3.connect(path)) as conn:
        with conn:
            yield conn


def write_table(conn, name, records, integers=()):
    columns = list(records[0])
    conn.execute("CREATE TABLE " + name + "(" + ",".join(
        column + (" INTEGER" if column in integers else " TEXT") for column in columns) + ")")
    conn.executemany("INSERT INTO " + name + " VALUES(" + ",".join("?" for _ in columns) + ")",
                     [tuple(row[column] for column in columns) for row in records])


def long_url(site, task_id, **overrides):
    query = dict(c=("yingliang_post_CLV_VL_999*1792371600noneen*English Drama*FBauto*1"
                    if site == "2049" else
                    "yingliang_post_CLV_VL_facebook_999*1792371600noneen*English Drama*none*1"),
                 af_adset="Frozen Page", af_adset_id="999", af_ad="Video_contentid[dramA]",
                 af_ad_id="55", af_channel="AIpost" if site == "2049" else "248",
                 af_c_id=task_id, af_dp="dramA")
    query.update(overrides)
    return "https://www.dramawavew2a.com/ads/" + ("0/2049" if site == "2049" else "101/2310") + "/view?" + urlencode(query)


class FacebookAttributionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.source, self.users, self.ledger = [self.root / name for name in ("fb.sqlite3", "users.sqlite3", "ledger.sqlite3")]
        self.snapshot = dict(
            actor=dict(tenant_key="tenant-A", user_id="creator", user_name="郜远", business_owner_id="248", sub_user_id="248"),
            attribution=dict(task_id=NEW_ID, page_id="999", page_name="Frozen Page", material_id="55",
                             material_name="Video", content_id="dramA", language="en", drama_name="English Drama",
                             tag="none", sub_user_id="248"),
            page=dict(page_id="999", page_name="Frozen Page"),
            material=dict(material_id="55", material_name="Video", content_id="dramA",
                          language="en", drama_name="English Drama"))
        with database(self.users) as conn:
            write_table(conn, "drama_admin_user", [
                dict(user_id="creator", tenant_key="tenant-A", name="郜远"),
                dict(user_id="editor", tenant_key="tenant-A", name="编辑人"),
                dict(user_id="runner", tenant_key="tenant-A", name="执行人")])
        with database(self.source) as conn:
            write_table(conn, "fb_auto_task", [dict(id=1, run_id=10, template_id=3, template_version=7,
                page_id="999", material_id="55", content_id="dramA", status="published",
                long_url=long_url("2049", "1"), short_url="https://gy.g2flow.com/s2l/fb/1.html",
                graph_post_id="999_100", unknown_outcome=0, created_at_utc=CREATED,
                completed_at_utc=PUBLISHED)], ("id", "run_id", "template_id", "template_version", "unknown_outcome"))
            write_table(conn, "fb_auto_template", [dict(id=3, owner_user_id="248", created_by="creator",
                created_name="郜远", created_at_utc=CREATED)], ("id",))
            write_table(conn, "fb_auto_template_version", [
                dict(template_id=3, version=1, created_by="creator", created_at_utc=CREATED),
                dict(template_id=3, version=7, created_by="editor", created_at_utc=LATER)], ("template_id", "version"))
            write_table(conn, "fb_auto_run", [dict(id=10, template_id=3, template_version=7,
                                                  trigger_type="manual")], ("id", "template_id", "template_version"))
            write_table(conn, "fb_auto_publish_ledger", [dict(task_id=1, page_id="999", material_id="55",
                status="published", graph_post_id="999_100", unknown_outcome=0,
                updated_at_utc=PUBLISHED)], ("task_id", "unknown_outcome"))
            write_table(conn, "fb_video_task", [dict(task_id=NEW_ID, batch_id="batch", tenant_key="tenant-A",
                owner_user_id="creator", page_id="999", material_id="55", content_id="dramA", status="published",
                snapshot_json=json.dumps(self.snapshot), ledger_id=1, long_url=long_url("2310", NEW_ID),
                short_url="https://gy.g2flow.com/s2l/facebook/1.html", graph_post_id="999_101",
                unknown_outcome=0, created_at_utc=CREATED, updated_at_utc=PUBLISHED)], ("ledger_id", "unknown_outcome"))
            write_table(conn, "fb_video_link", [dict(id=1, task_id=NEW_ID, long_url=long_url("2310", NEW_ID),
                short_url="https://gy.g2flow.com/s2l/facebook/1.html", created_at_utc=CREATED)], ("id",))
            write_table(conn, "fb_video_batch", [dict(batch_id="batch", tenant_key="tenant-A",
                owner_user_id="creator", owner_name="郜远", operation_id="operation", created_at_utc=CREATED)])

    def tearDown(self):
        self.temp.cleanup()

    def run_collect(self, now=NOW, **kwargs):
        return fb.collect(self.source, self.users, self.ledger, now=now, **kwargs)

    def read(self, now=NOW):
        return fb.read_facebook(dict(SOCIAL_ANALYTICS_FACEBOOK_ATTRIBUTION_DB=self.ledger,
                                     SOCIAL_ANALYTICS_FACEBOOK_NOW=now))

    def change(self, sql, args=(), path=None):
        with database(path or self.source) as conn:
            conn.execute(sql, args)

    def saved(self, site="2049"):
        with database(self.ledger) as conn:
            conn.row_factory = sqlite3.Row
            return dict(conn.execute("SELECT * FROM social_facebook_identity WHERE site_id=?", (site,)).fetchone())

    def test_full_snapshot_attribution_uses_original_creator_and_manual_submitter(self):
        result = self.run_collect(mode="full")
        self.assertEqual(result["status"], "complete")
        catalog, errors, meta = self.read()
        self.assertFalse(errors)
        self.assertEqual({row["owner"] for row in catalog}, {"creator"})
        self.assertEqual({row["tenant"] for row in catalog}, {"tenant-A"})
        self.assertEqual({row["owner_label"] for row in catalog}, {"郜远"})
        self.assertEqual({row["sub_user_id"] for row in catalog}, {"248"})
        self.assertEqual({row["site_id"] for row in catalog}, {"2049", "2310"})
        self.assertEqual({row["channel"] for row in catalog}, {"facebook:999"})
        self.assertEqual({row["drama_label"] for row in catalog}, {"English Drama"})
        self.assertTrue(all(row["publications"][0]["date"] == "2026-10-19" for row in catalog))
        self.assertTrue(all(value["freshness"] == "fresh" for value in meta.values()))

    def test_initial_empty_new_source_is_complete_not_failure(self):
        self.change("DELETE FROM fb_video_task")
        self.change("DELETE FROM fb_video_link")
        result = self.run_collect()
        self.assertEqual(result["sources"]["2310"]["count"], 0)
        self.assertEqual(self.read()[2]["facebook:2310"]["state"], "complete")

    def test_repeat_freeze_and_directory_changes_do_not_reassign(self):
        self.run_collect()
        before = self.saved()
        self.change("UPDATE drama_admin_user SET tenant_key='tenant-OTHER',name='Changed' WHERE user_id='creator'", path=self.users)
        result = self.run_collect(now=LATER)
        self.assertEqual(result["sources"]["2049"]["changed"], 0)
        self.assertEqual(self.saved(), before)
        catalog = self.read(now=LATER)[0]
        self.assertTrue(all(row["tenant"] == "tenant-A" and row["owner_label"] == "郜远" for row in catalog))

    def test_current_editor_and_manual_runner_do_not_replace_old_creator(self):
        self.change("UPDATE fb_auto_template_version SET created_by='runner' WHERE version=7")
        self.run_collect()
        old = next(row for row in self.read()[0] if row["site_id"] == "2049")
        self.assertEqual(old["owner"], "creator")
        self.assertEqual(old["link_type"], "auto_template")

    def test_template_first_version_mismatch_unresolved_and_no_248_fallback(self):
        self.change("UPDATE fb_auto_template_version SET created_by='editor' WHERE version=1")
        result = self.run_collect()
        self.assertEqual(result["sources"]["2049"]["conflicts"], 1)
        old = next(row for row in self.read()[0] if row["site_id"] == "2049")
        self.assertEqual(old["owner"], "")
        self.assertEqual(old["owner_label"], "待归属")

    def test_unmapped_creator_does_not_default_to_248(self):
        self.change("DELETE FROM drama_admin_user WHERE user_id='creator'", path=self.users)
        self.run_collect()
        self.assertTrue(all(row["owner"] == "" for row in self.read()[0]))

    def test_legacy_creator_across_tenants_is_ambiguous_but_new_explicit_tenant_is_valid(self):
        self.change("INSERT INTO drama_admin_user VALUES('creator','tenant-B','Another')", path=self.users)
        self.run_collect()
        catalog = {row["site_id"]: row for row in self.read()[0]}
        self.assertEqual(catalog["2049"]["owner"], "")
        self.assertEqual(catalog["2310"]["owner"], "creator")
        self.assertEqual(catalog["2310"]["tenant"], "tenant-A")

    def test_wrong_new_batch_tenant_does_not_join_by_owner_alone(self):
        self.change("UPDATE fb_video_batch SET tenant_key='tenant-B'")
        self.run_collect()
        new = next(row for row in self.read()[0] if row["site_id"] == "2310")
        self.assertEqual(new["owner"], "")

    def test_new_snapshot_actor_and_link_must_agree(self):
        for mutation in (
                lambda saved: saved["actor"].update(user_id="runner"),
                lambda saved: saved["attribution"].update(sub_user_id="999"),
                lambda saved: saved["material"].update(content_id="other")):
            with self.subTest(mutation=mutation):
                saved = copy.deepcopy(self.snapshot)
                mutation(saved)
                self.change("UPDATE fb_video_task SET snapshot_json=?", (json.dumps(saved),))
                self.run_collect()
                self.assertEqual(self.saved("2310")["status"], "unresolved")

    def test_new_link_mismatch_and_duplicate_query_rejected(self):
        bad = long_url("2310", NEW_ID) + "&af_c_id=another"
        self.change("UPDATE fb_video_task SET long_url=?", (bad,))
        self.change("UPDATE fb_video_link SET long_url=?", (bad,))
        self.run_collect()
        self.assertEqual(self.saved("2310")["status"], "unresolved")

    def test_case_sensitive_campaign_id_and_exact_path(self):
        with self.assertRaises(fb.AttributionError):
            fb.parse_link(long_url("2310", "ABC"), "2310", "abc")
        with self.assertRaises(fb.AttributionError):
            fb.parse_link(long_url("2049", "1").replace("/ads/0/", "/ads/101/"), "2049", "1")
        self.assertEqual(fb.parse_link(long_url("2310", "AbC"), "2310", "AbC")["af_c_id"], "AbC")

    def test_published_confirmation_uses_terminal_time_not_created(self):
        self.change("UPDATE fb_video_task SET updated_at_utc='2026-10-20T03:00:00Z'")
        self.run_collect()
        new = next(row for row in self.read()[0] if row["site_id"] == "2310")
        self.assertEqual(new["publications"][0]["date"], "2026-10-20")

    def test_submitted_and_unknown_never_count_as_published(self):
        self.change("UPDATE fb_auto_task SET status='submitted'")
        self.change("UPDATE fb_video_task SET status='unknown',unknown_outcome=1")
        self.run_collect()
        self.assertTrue(all(not row["publications"] for row in self.read()[0]))

    def test_legacy_published_requires_matching_ledger(self):
        self.change("UPDATE fb_auto_publish_ledger SET graph_post_id='999_777'")
        self.run_collect()
        old = next(row for row in self.read()[0] if row["site_id"] == "2049")
        self.assertFalse(old["publications"])
        self.assertEqual(old["owner"], "")

    def test_pending_task_can_gain_confirmed_fact_without_reassigning_owner(self):
        self.change("UPDATE fb_video_task SET status='queued',graph_post_id=''")
        self.run_collect()
        before = self.saved("2310")
        self.change("DELETE FROM drama_admin_user WHERE user_id='creator'", path=self.users)
        self.change("UPDATE fb_video_task SET status='published',graph_post_id='999_101',updated_at_utc=?", (PUBLISHED,))
        self.run_collect(now=LATER)
        after = self.saved("2310")
        self.assertEqual(before["identity_json"], after["identity_json"])
        self.assertEqual(after["status"], "frozen")
        self.assertEqual(len(json.loads(after["publications_json"])), 1)

    def test_identity_drift_is_sticky_conflict_preserves_original_evidence(self):
        self.run_collect()
        before = self.saved()
        self.change("UPDATE fb_auto_template SET created_by='editor'")
        self.run_collect(now=LATER)
        after = self.saved()
        self.assertEqual(after["status"], "conflict")
        self.assertEqual(after["identity_json"], before["identity_json"])
        self.assertEqual(after["evidence_hash"], before["evidence_hash"])
        self.assertNotEqual(after["observed_hash"], before["observed_hash"])
        self.change("UPDATE fb_auto_template SET created_by='creator'")
        self.run_collect(now=LATER)
        self.assertEqual(self.saved()["status"], "conflict")
        self.assertEqual(next(row for row in self.read(now=LATER)[0] if row["site_id"] == "2049")["owner"], "")

    def test_confirmed_fact_mutation_does_not_silently_rewrite_history(self):
        self.run_collect()
        before = self.saved("2310")
        self.change("UPDATE fb_video_task SET graph_post_id='999_888'")
        self.run_collect(now=LATER)
        after = self.saved("2310")
        self.assertEqual(after["status"], "conflict")
        self.assertEqual(after["publications_json"], before["publications_json"])

    def test_deleted_record_remains_conflict_not_removed(self):
        self.run_collect()
        self.change("DELETE FROM fb_auto_task")
        self.run_collect(now=LATER)
        self.assertEqual(self.saved()["error_code"], "source_record_missing")

    def test_unresolved_can_freeze_after_evidence_becomes_unique(self):
        self.change("DELETE FROM drama_admin_user WHERE user_id='creator'", path=self.users)
        self.run_collect()
        self.assertEqual(self.saved()["frozen_at"], "")
        self.change("INSERT INTO drama_admin_user VALUES('creator','tenant-A','郜远')", path=self.users)
        self.run_collect(now=LATER)
        self.assertEqual(self.saved()["status"], "frozen")
        self.assertEqual(self.saved()["frozen_at"], LATER)

    def test_failure_keeps_prior_snapshot_marks_stale_and_other_site_survives(self):
        self.run_collect()
        before = self.saved()
        self.change("DROP TABLE fb_auto_template_version")
        result = self.run_collect(now=LATER)
        self.assertEqual(result["sources"]["2049"]["status"], "stale")
        self.assertEqual(result["sources"]["2310"]["status"], "complete")
        self.assertEqual(self.saved(), before)
        catalog, errors, meta = self.read(now=LATER)
        self.assertEqual(len(catalog), 2)
        self.assertFalse(errors)
        self.assertEqual(meta["facebook:2049"]["freshness"], "stale")
        self.assertEqual(meta["facebook:2049"]["state"], "partial")
        self.assertEqual(meta["facebook:2310"]["state"], "complete")

    def test_user_query_failure_not_no_relationship(self):
        self.run_collect()
        self.change("DROP TABLE drama_admin_user", path=self.users)
        result = self.run_collect(now=LATER)
        self.assertTrue(all(value["status"] == "stale" for value in result["sources"].values()))
        self.assertTrue(all(row["owner"] == "creator" for row in self.read(now=LATER)[0]))

    def test_first_query_failure_unavailable_and_missing_db_is_not_created(self):
        missing = self.root / "does-not-exist.sqlite3"
        fb.collect(missing, self.users, self.ledger, now=NOW)
        catalog, errors, meta = self.read()
        self.assertFalse(catalog)
        self.assertEqual(set(errors), {"facebook:2049", "facebook:2310"})
        self.assertTrue(all(value["state"] == "unavailable" for value in meta.values()))
        self.assertFalse(missing.exists())

    def test_freshness_five_minutes_and_clock_skew(self):
        self.run_collect()
        self.assertEqual(self.read("2026-10-20T09:05:00Z")[2]["facebook:2049"]["state"], "complete")
        for now in ("2026-10-20T09:05:01Z", "2026-10-20T08:59:59Z"):
            self.assertEqual(self.read(now)[2]["facebook:2049"]["freshness"], "stale")
            self.assertEqual(len(self.read(now)[0]), 2)

    def test_atomic_failure_rolls_back_rows_but_records_stale_attempt(self):
        self.run_collect()
        before = self.saved()
        self.change("UPDATE fb_auto_template SET created_by='editor'")
        original = fb._upsert_record

        def fail_second(conn, site, record, users, now):
            if site == "2310":
                raise RuntimeError("simulated interrupted collection")
            return original(conn, site, record, users, now)

        with patch.object(fb, "_upsert_record", fail_second):
            with self.assertRaises(RuntimeError):
                self.run_collect(now=LATER)
        self.assertEqual(self.saved(), before)
        self.assertTrue(all(value["freshness"] == "stale" for value in self.read(now=LATER)[2].values()))

    def test_sources_and_api_reader_do_not_mutate_databases(self):
        before = {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in (self.source, self.users)}
        self.run_collect()
        self.run_collect(now=LATER)
        after = {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in before}
        self.assertEqual(before, after)
        ledger_hash = hashlib.sha256(self.ledger.read_bytes()).hexdigest()
        self.read(now=LATER)
        self.assertEqual(ledger_hash, hashlib.sha256(self.ledger.read_bytes()).hexdigest())

    def test_reader_does_not_create_missing_ledger(self):
        self.assertEqual(len(self.read()[1]), 2)
        self.assertFalse(self.ledger.exists())

    def test_ledger_and_lock_cannot_be_source_database(self):
        with self.assertRaises(fb.AttributionError):
            fb.collect(self.source, self.users, self.source, now=NOW)
        with self.assertRaises(fb.AttributionError):
            self.run_collect(lock_path=self.users)

    def test_lock_rejects_concurrent_collector_and_releases_after_failure(self):
        lock = self.root / "lock"
        with fb.collector_lock(lock):
            with self.assertRaisesRegex(fb.AttributionError, "collector_busy"):
                self.run_collect(lock_path=lock)
        self.assertEqual(self.run_collect(lock_path=lock)["status"], "complete")

    def test_same_campaign_id_different_sites_remains_site_scoped(self):
        self.run_collect()
        with database(self.ledger) as conn:
            rows = conn.execute("SELECT site_id,identity_json FROM social_facebook_identity").fetchall()
            for site, payload in rows:
                identity = json.loads(payload)
                identity["key"][0] = "SameCaseID"
                conn.execute("UPDATE social_facebook_identity SET identity_json=? WHERE site_id=?",
                             (json.dumps(identity), site))
        catalog = self.read()[0]
        self.assertEqual(len({(row["site_id"], row["key"][0]) for row in catalog}), 2)
        self.assertEqual(len({row["link_id"] for row in catalog}), 2)

    def test_foreign_ledger_version_fails_closed(self):
        self.run_collect()
        self.change("UPDATE social_facebook_schema SET version='future-v99'", path=self.ledger)
        self.assertEqual(len(self.read()[1]), 2)

    def test_foreign_row_version_fails_collector_without_claiming_success(self):
        self.run_collect()
        self.change("UPDATE social_facebook_identity SET version='unknown' WHERE site_id='2049'", path=self.ledger)
        with self.assertRaisesRegex(fb.AttributionError, "ledger_version_unsupported"):
            self.run_collect(now=LATER)
        self.assertEqual(self.read(now=LATER)[2]["facebook:2310"]["freshness"], "stale")

    def test_unrecognized_publication_status_is_pending_not_false_zero(self):
        self.change("UPDATE fb_video_task SET status='new-unrecognized-status'")
        self.run_collect()
        self.assertEqual(self.saved("2310")["error_code"], "publication_status_invalid")
        self.assertEqual(self.read()[2]["facebook:2310"]["state"], "partial")

    def test_cli_reports_safe_failure_exit_status(self):
        from contextlib import redirect_stdout
        from io import StringIO
        from scripts.social_facebook_attribution import main
        output = StringIO()
        with redirect_stdout(output):
            status = main(["--fb-db", str(self.root / "missing"), "--users-db", str(self.users),
                           "--ledger", str(self.ledger)])
        payload = json.loads(output.getvalue())
        self.assertEqual(status, 1)
        self.assertEqual(payload["status"], "partial")
        self.assertNotIn(str(self.root), output.getvalue())
        self.assertNotIn("creator", output.getvalue())

    def test_confirmed_conflict_keeps_fact_for_shared_pending_count(self):
        self.run_collect()
        self.change("UPDATE fb_auto_template SET created_by='editor'")
        self.run_collect(now=LATER)
        row = next(row for row in self.read(now=LATER)[0] if row["site_id"] == "2049")
        self.assertEqual(row["owner"], "")
        self.assertEqual(len(row["publications"]), 1)

    def test_post_object_deduplication_is_scoped_to_page_and_normalized(self):
        captured, _ = fb.source_snapshot(self.source)
        first = captured["2310"][0]
        first["facts"]["graph_post_id"] = "101"
        normalized = fb.publications("2310", first)[0]["id"]
        first["facts"]["graph_post_id"] = "999_101"
        self.assertEqual(fb.publications("2310", first)[0]["id"], normalized)
        first["evidence"]["task"]["page_id"] = "888"
        first["facts"]["graph_post_id"] = "101"
        self.assertNotEqual(fb.publications("2310", first)[0]["id"], normalized)
        first["facts"]["graph_post_id"] = "999_101"
        with self.assertRaisesRegex(fb.AttributionError, "publication_page_mismatch"):
            fb.publications("2310", first)

    @unittest.skipIf(os.name == "nt", "POSIX file permissions")
    def test_private_ledger_and_lock_do_not_change_parent_permissions(self):
        original = stat.S_IMODE(self.root.stat().st_mode)
        self.run_collect()
        for path in (self.ledger, self.ledger.with_suffix(".collector.lock")):
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.root.stat().st_mode), original)

    def test_unsupported_or_malformed_snapshot_fails_one_record(self):
        self.change("UPDATE fb_video_task SET snapshot_json='[]'")
        result = self.run_collect()
        self.assertEqual(result["sources"]["2049"]["status"], "complete")
        self.assertEqual(result["sources"]["2310"]["status"], "partial")

    @unittest.skipIf(os.name == "nt", "POSIX descriptor locks")
    def test_collect_does_not_open_or_close_sqlite_owned_sidecars(self):
        original = fb._private_open
        def safe(path):
            self.assertFalse(str(path).endswith(('-wal', '-shm')),
                             'closing a sidecar FD can release SQLite locks')
            return original(path)
        with patch.object(fb, '_private_open', side_effect=safe):
            self.assertEqual(self.run_collect()['status'], 'complete')

    @unittest.skipIf(os.name == "nt", "POSIX descriptor locks")
    def test_sidecar_permission_tightening_keeps_existing_process_lock(self):
        import fcntl
        import subprocess
        import sys
        path = self.root / 'guard-shm'
        path.write_bytes(b'lock evidence')
        path.chmod(0o666)
        with path.open('r+b') as handle:
            fcntl.lockf(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fb._tighten_sidecar_mode(path)
            script = """import fcntl,sys
h=open(sys.argv[1],'r+b')
try: fcntl.lockf(h,fcntl.LOCK_EX|fcntl.LOCK_NB)
except BlockingIOError: sys.exit(0)
sys.exit(1)
"""
            self.assertEqual(subprocess.run([sys.executable, '-c', script, str(path)],
                                            timeout=3).returncode, 0)
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(path.read_bytes(), b'lock evidence')


if __name__ == "__main__":
    unittest.main()
