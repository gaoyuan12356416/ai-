"""The isolated old selector ABI must retain TT's strict eligibility checks."""
from datetime import datetime, timezone
import unittest
from unittest.mock import patch

from features.tt_posts.service import _TTDramawaveCandidateSelector, DramawaveMaterialResolver, TTPostServiceError
from features.x_posts.selector import DramawaveCandidateSelector
from scripts import test_tt_posts_service as fixtures


class ValidatorCompatibilityTests(unittest.TestCase):
    def test_old_two_argument_selector_contract_and_strict_tt_material_rules(self):
        modern=DramawaveCandidateSelector._pool_candidate
        calls=[]
        def legacy(self,material_id,source_date):
            # Old ABI, with its strict eligibility semantics. Modern shared
            # validation is used behind this fixture to exercise real TT rows.
            calls.append(material_id)
            return modern(self,material_id,source_date,max_duration_seconds=3600,
                allow_images=False,allow_deleted_materials=False,require_dramawave_product=True,use_drama_language=False)
        for overrides,expected in (({},None),({"material_type":1},"material_type_not_video"),
                ({"is_delete":1},"material_deleted"),({"video_duration":3601},"material_duration_out_of_range"),
                ({"product":"Other"},"material_product_mismatch")):
            with self.subTest(overrides=overrides):
                connection,_=fixtures.MaterialResolverTests._material_connection([fixtures.MaterialResolverTests._material_row(**overrides)])
                resolver=DramawaveMaterialResolver(lambda:connection,now_fn=lambda:datetime(2026,7,29,tzinfo=timezone.utc))
                with patch.object(DramawaveCandidateSelector,"_pool_candidate",legacy), \
                     patch.object(_TTDramawaveCandidateSelector,"_violation_counts",return_value={k:0 for k in ("facebook_count","tiktok_count","twitter_count","resource_audit_count")}), \
                     patch.object(_TTDramawaveCandidateSelector,"_material_tags",return_value=[]), \
                     patch.object(_TTDramawaveCandidateSelector,"_pool_drama_rows",return_value=[{"content_id":"ABCD1234","series_code":"series","language":"English","drama_name":"Drama","drama_labels":"romance","drama_description":"Description"}]), \
                     patch.object(_TTDramawaveCandidateSelector,"_validate_drama_deploy_time",return_value=0):
                    if expected:
                        with self.assertRaises(TTPostServiceError) as caught:resolver.resolve("9001")
                        self.assertEqual(caught.exception.code,expected)
                    else:
                        self.assertEqual(resolver.resolve("9001")["material_id"],"9001")
        self.assertEqual(calls,["9001"]*5)

    def test_modern_contract_receives_explicit_tt_policy(self):
        calls=[]
        def modern(self,material_id,source_date,**options):
            calls.append(options);return {"material_id":material_id}
        with patch.object(DramawaveCandidateSelector,"_pool_candidate",modern):
            selector=object.__new__(_TTDramawaveCandidateSelector)
            selector._pool_candidate("9001","2026-11-02",allow_images=True,allow_deleted_materials=True)
        self.assertEqual(calls,[{"max_duration_seconds":3600,"allow_images":False,"allow_deleted_materials":False,"require_dramawave_product":True,"use_drama_language":False}])

    def test_old_cursor_contract_preserves_all_six_violation_bindings(self):
        calls=[]
        def legacy(connection,sql,params):
            calls.append((sql,params));return {"facebook_count":0,"tiktok_count":0,"twitter_count":1,"resource_audit_count":0}
        selector=object.__new__(_TTDramawaveCandidateSelector)
        selector.connection=object();selector.schema="fixture"
        with patch("features.x_posts.selector._cursor_row",legacy):
            result=selector._violation_counts("9001")
        self.assertEqual(result["twitter_count"],1)
        self.assertEqual(calls[0][1],("9001",)*6)
        self.assertIn("original_source_id",calls[0][0])


if __name__=="__main__":unittest.main()
