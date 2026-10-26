"""Execute Page authorization predicates against isolated relational fixtures."""
import re
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from features.fb_video_publish.core import Store
from features.fb_video_publish.query_broker import validate_query
from features.fb_video_publish.service import Service, PREFIX
from features.fb_video_publish.sources import SourceError, SourceRepository


class AuthorizationDB:
    schema, blacklist_schema = "kunlunads_dev", "ads_setting"

    def __init__(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.create_function("CONCAT", -1, lambda *parts: "".join(parts))
        self.conn.create_function("LOCATE", 2, lambda needle, text: text.find(needle) + 1)
        self.calls = []
        # Empty Page pools prove personal access does not require membership.
        self.conn.executescript("""
            CREATE TABLE ads_facebook_info(user_id TEXT, facebookUserID TEXT);
            CREATE TABLE ads_facebook_page_ins(page_id TEXT, user_id INTEGER);
            CREATE TABLE ads_facebook_page_post(page_id TEXT, fb_user_id TEXT,
                page_name TEXT, language TEXT, status INTEGER, page_access_token TEXT);
            CREATE TABLE ads_facebook_page_group(id INTEGER, user_id TEXT, app_id INTEGER,
                type INTEGER, is_delete INTEGER);
            CREATE TABLE ads_apps_setting(id INTEGER, name TEXT);
            CREATE TABLE ads_facebook_page_group_ins(group_id INTEGER, page_id TEXT, deleted_at INTEGER);
            INSERT INTO ads_apps_setting VALUES (1479,'Dramawave'),(999,'Other');
            INSERT INTO ads_facebook_info VALUES ('803','fb-A'),('804','fb-B'),('805','fb-a');
            INSERT INTO ads_facebook_page_post VALUES
                ('1481898564996632','fb-A','Billionaire Drama Club','en',-1,'fixture-a'),
                ('1481898564996632','fb-B','Billionaire Drama Club','en',-1,'fixture-b'),
                ('1437464599440691','fb-B','Other owner only','en',0,'fixture-c'),
                ('1303194919554167','fb-A','Revoked','en',1,'fixture-d'),
                ('1303194919554167','fb-B','Revoked','en',0,'fixture-e'),
                ('1357160340818434','fb-A','Empty credential','en',-1,'  '),
                ('1418318351365171','unmapped','Unmapped Facebook identity','en',0,'fixture-f');
        """)

    def select(self, sql, params):
        validate_query(dict(sql=sql, params=list(params), schema=self.schema,
                            blacklist_schema=self.blacklist_schema))
        self.calls.append((sql, list(params)))
        # Only adapt MySQL syntax; execute the real scope, grouping, count and
        # search predicates. SQLite CAST AS BINARY is numeric, not a byte compare.
        compatible = sql.replace("`kunlunads_dev`.", "").replace("%s", "?")
        compatible = re.sub(r"CAST\((\w+\.\w+) AS BINARY\)", r"\1 COLLATE BINARY", compatible)
        return [dict(row) for row in self.conn.execute(compatible, params)]


class AuthorizedPageTests(unittest.TestCase):
    def setUp(self):
        self.db = AuthorizationDB()
        self.addCleanup(self.db.conn.close)
        self.sources = SourceRepository(None, SimpleNamespace(mysql=self.db))
        self.actor = dict(tenant_key="t", user_id="feishu-A", user_name="A",
                          business_owner_id="803", sub_user_id="803", is_admin=False)

    def test_no_pool_required_and_only_personal_authorizations_are_visible(self):
        pages = self.sources.list_pages(self.actor)
        self.assertEqual({p["page_id"] for p in pages},
                         {"1481898564996632", "1303194919554167", "1357160340818434"})
        page = self.sources.get_pages(self.actor, ["1481898564996632"])["1481898564996632"]
        self.assertEqual(page["eligible_token_count"], 1)
        self.assertEqual(page["group_ids"], [])
        self.assertFalse(set(page) & {"token", "page_access_token", "fb_user_id"})

    def test_other_owner_does_not_leak_through_search_or_direct_selection(self):
        for query in ("1437464599440691", "Other owner", "Unmapped"):
            self.assertEqual(self.sources.list_pages(self.actor, query), [])
        with self.assertRaises(SourceError) as denied:
            self.sources.get_pages(self.actor, ["1437464599440691"])
        self.assertEqual(denied.exception.code, "page_not_found")
        # Facebook identity matching remains case-sensitive.
        self.assertEqual(self.sources.list_pages(dict(self.actor, business_owner_id="805")), [])

    def test_revoked_personal_token_cannot_use_other_owners_eligibility(self):
        for pid in ("1303194919554167", "1357160340818434"):
            page = self.sources.list_pages(self.actor, pid)[0]
            self.assertEqual(page["eligible_token_count"], 0)
            self.assertTrue(page["blocked_reason"])
            with self.assertRaises(SourceError) as denied:
                self.sources.get_pages(self.actor, [pid])
            self.assertEqual(denied.exception.code, "page_unavailable")

    def test_admin_keeps_global_scope_and_deduplicates(self):
        admin = dict(self.actor, is_admin=True, business_owner_id="", sub_user_id="")
        pages = self.sources.list_pages(admin)
        self.assertEqual(len(pages), 5)
        page = self.sources.list_pages(admin, "1481898564996632")[0]
        self.assertEqual(page["eligible_token_count"], 2)

    def test_missing_mapping_fails_closed_before_sql(self):
        with self.assertRaises(SourceError) as denied:
            self.sources.list_pages(dict(self.actor, business_owner_id=""))
        self.assertEqual(denied.exception.status, 403)
        self.assertEqual(self.db.calls, [])

    def test_search_filters_in_sql_and_escapes_metacharacters(self):
        self.assertEqual(len(self.sources.list_pages(self.actor, "1481898564996632")), 1)
        sql, params = self.db.calls[-1]
        self.assertIn("p.page_id=%s", sql)
        self.assertEqual(params, ["803", "803", "803", "1481898564996632"])
        self.assertEqual(self.sources.list_pages(self.actor, "148189856499663"), [])
        self.assertEqual(len(self.sources.list_pages(self.actor, "BILLIONAIRE")), 1)
        for query in ("%", "' OR 1=1 --", "_"):
            self.assertEqual(self.sources.list_pages(self.actor, query), [])

    def test_revocation_is_live_and_configuration_uses_same_scope(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "store.sqlite")
            service = Service(store, self.sources, "x" * 48, live_enabled=True)
            pid = "1481898564996632"
            service.handle("POST", PREFIX + "/pages", self.actor,
                           dict(page_ids=[pid], language="en", caption_template="{name} {url}"))
            self.assertEqual([p["page_id"] for p in service.configured_pages(self.actor)["items"]], [pid])
            outsider = dict(self.actor, user_id="other", business_owner_id="805", sub_user_id="805")
            self.assertEqual(service.configured_pages(outsider)["items"], [])
            with self.assertRaises(Exception):
                service.handle("POST", PREFIX + "/pages", outsider,
                               dict(page_ids=[pid], language="en", caption_template="{name} {url}"))
            self.db.conn.execute("UPDATE ads_facebook_page_post SET status=1 WHERE fb_user_id='fb-A'")
            with self.assertRaises(SourceError) as revoked:
                self.sources.get_pages(self.actor, [pid])
            self.assertEqual(revoked.exception.code, "page_unavailable")
            with store.connect() as conn:
                for table in ("fb_video_task", "fb_video_link", "fb_video_attempt"):
                    self.assertEqual(conn.execute("SELECT COUNT(*) FROM " + table).fetchone()[0], 0)

    def test_existing_delegated_pool_grants_survive_without_personal_token(self):
        self.db.conn.executescript("""
            INSERT INTO ads_facebook_page_group VALUES (1,'841',1479,0,0);
            INSERT INTO ads_facebook_page_group_ins VALUES (1,'1481898564996632',0);
        """)
        delegated = dict(self.actor, business_owner_id="841", sub_user_id="841")
        page = self.sources.get_pages(delegated, ["1481898564996632"])["1481898564996632"]
        self.assertEqual(page["eligible_token_count"], 2)
        self.assertEqual(self.sources.list_pages(delegated, "Other owner"), [])
        # Deleted membership, deleted group, unrelated product and owner do not
        # grant access. Restore each fixture before checking the next guard.
        for update, restore in (
            ("UPDATE ads_facebook_page_group_ins SET deleted_at=1", "UPDATE ads_facebook_page_group_ins SET deleted_at=0"),
            ("UPDATE ads_facebook_page_group SET is_delete=1", "UPDATE ads_facebook_page_group SET is_delete=0"),
            ("UPDATE ads_facebook_page_group SET app_id=999", "UPDATE ads_facebook_page_group SET app_id=1479"),
            ("UPDATE ads_facebook_page_group SET user_id='800'", "UPDATE ads_facebook_page_group SET user_id='841'"),
        ):
            self.db.conn.execute(update)
            self.assertEqual(self.sources.list_pages(delegated), [])
            self.db.conn.execute(restore)

    def test_page_responsible_user_can_search_without_personal_authorization_or_pool(self):
        pid = "1418318351365171"
        self.db.conn.execute("INSERT INTO ads_facebook_page_ins VALUES (?,?)", (pid, 904))
        owner = dict(self.actor, business_owner_id="904", sub_user_id="904")
        for query in ("", pid, "Unmapped Facebook identity"):
            pages = self.sources.list_pages(owner, query)
            self.assertEqual([p["page_id"] for p in pages], [pid])
        self.assertEqual(self.sources.get_pages(owner, [pid])[pid]["eligible_token_count"], 1)
        self.assertEqual(self.sources.list_pages(self.actor, pid), [])
        other = dict(owner, business_owner_id="905", sub_user_id="905")
        with self.assertRaises(SourceError) as denied:
            self.sources.get_pages(other, [pid])
        self.assertEqual(denied.exception.code, "page_not_found")

    def test_page_responsibility_changes_revoke_access_and_filter_shared_configs(self):
        pid = "1418318351365171"
        self.db.conn.execute("INSERT INTO ads_facebook_page_ins VALUES (?,?)", (pid, 904))
        owner = dict(self.actor, user_id="assigned-owner", business_owner_id="904", sub_user_id="904")
        successor = dict(owner, user_id="successor", business_owner_id="905", sub_user_id="905")
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "store.sqlite")
            service = Service(store, self.sources, "x" * 48, live_enabled=True)
            service.handle("POST", PREFIX + "/pages", owner,
                           dict(page_ids=[pid], language="en", caption_template="{name} {url}"))
            self.assertEqual([p["page_id"] for p in service.configured_pages(owner)["items"]], [pid])
            self.assertEqual(service.configured_pages(dict(owner, tenant_key="other-tenant"))["items"], [])
            self.db.conn.execute("UPDATE ads_facebook_page_ins SET user_id=905 WHERE page_id=?", (pid,))
            self.assertEqual(service.configured_pages(owner)["items"], [])
            self.assertEqual([p["page_id"] for p in service.configured_pages(successor)["items"]], [pid])
            with self.assertRaises(SourceError):
                self.sources.get_pages(owner, [pid])
            self.db.conn.execute("DELETE FROM ads_facebook_page_ins WHERE page_id=?", (pid,))
            self.assertEqual(self.sources.list_pages(successor, pid), [])

    def test_page_assignment_does_not_create_authorization_or_bypass_token_eligibility(self):
        owner = dict(self.actor, business_owner_id="904", sub_user_id="904")
        missing, empty = "9999999999999999", "1357160340818434"
        self.db.conn.executemany("INSERT INTO ads_facebook_page_ins VALUES (?,?)",
                                [(missing, 904), (empty, 904)])
        self.assertEqual(self.sources.list_pages(owner, missing), [])
        page = self.sources.list_pages(owner, empty)[0]
        self.assertEqual(page["eligible_token_count"], 0)
        self.assertTrue(page["blocked_reason"])
        with self.assertRaises(SourceError) as unavailable:
            self.sources.get_pages(owner, [empty])
        self.assertEqual(unavailable.exception.code, "page_unavailable")

    def test_overlapping_owner_grants_do_not_duplicate_pages_or_credentials(self):
        pid = "1481898564996632"
        self.db.conn.executescript("""
            INSERT INTO ads_facebook_page_group VALUES (1,'803',1479,0,0);
            INSERT INTO ads_facebook_page_group_ins VALUES (1,'1481898564996632',0);
            INSERT INTO ads_facebook_page_ins VALUES ('1481898564996632',803);
            INSERT INTO ads_facebook_page_ins VALUES ('1481898564996632',803);
        """)
        pages = self.sources.list_pages(self.actor, pid)
        self.assertEqual(len(pages), 1)
        self.assertEqual(pages[0]["eligible_token_count"], 2)
        self.db.conn.execute("DELETE FROM ads_facebook_page_ins")
        self.assertEqual(self.sources.get_pages(self.actor, [pid])[pid]["eligible_token_count"], 2)


if __name__ == "__main__":
    unittest.main()
