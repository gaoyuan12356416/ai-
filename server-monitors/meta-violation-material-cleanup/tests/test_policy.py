import importlib.util
import json
from pathlib import Path
import unittest


_PATH = Path(__file__).resolve().parents[1] / "meta_violation_cleanup" / "policy.py"
_SPEC = importlib.util.spec_from_file_location("cleanup_policy_tested", _PATH)
policy = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(policy)


def lineage(ad_id="100", material="501", creative="900", product="1479"):
    return {"ad_id": ad_id, "product": product, "source_id": "0",
            "original_source_id": material, "creative_id": creative}


def live_ad(ad_id="100", creative="900"):
    return {"id": ad_id, "account_id": "77", "status": "ACTIVE", "effective_status": "ACTIVE",
            "creative": {"id": creative}, "campaign": {"id": "1", "status": "ACTIVE"},
            "adset": {"id": "2", "status": "ACTIVE"}}


def evidence(count=5, mid="501"):
    return {"materials": {mid: {"count": count, "violating_ad_ids": [str(200 + n) for n in range(count)],
                                "policies": ["Spam"], "root_ids": ["1479"]}}, "excluded": []}


class PolicyTests(unittest.TestCase):
    def setUp(self):
        self.scope = policy.product_scope([
            {"id": 1723, "ads_app_id": 1479}, {"id": 3303, "ads_app_id": 3302},
            {"id": 9999, "ads_app_id": 1723}, {"id": 827, "ads_app_id": 826}])

    def identity(self, **kwargs):
        return policy.material_identity([lineage(**kwargs)], {}, self.scope, creative_id=kwargs.get("creative", "900"))

    def test_product_scope_roots_direct_children_only(self):
        self.assertEqual(self.scope, {"1479": "1479", "3302": "3302", "1723": "1479", "3303": "3302"})
        self.assertEqual(policy.ROOT_PRODUCTS, {1479: "DramaWave", 3302: "HotDrama"})
        self.assertEqual(len(policy.POLICY_NAMES), 7)

    def test_product_scope_conflicting_configuration_excluded(self):
        scope = policy.product_scope([{"id": 55, "ads_app_id": 1479}, {"id": 55, "ads_app_id": 3302}])
        self.assertNotIn("55", scope)

    def test_parse_strict_ids(self):
        self.assertEqual(policy.parse_ids(' ["12", 2, "12"] '), ["2", "12"])
        self.assertEqual(policy.parse_ids("12, 2,12"), ["2", "12"])
        for bad in ("0", "1,0", "1,", "-1", "01", '[1,true]', '[1,2.0]', '{"id":1}', "1;2", "1" * 768):
            with self.subTest(bad=str(bad)[:30]):
                with self.assertRaises(ValueError):
                    policy.parse_ids(bad)

    def test_source_mapping_cross_checks_original(self):
        row = lineage(material="501,502")
        row["source_id"] = "10,11"
        sources = {"10": {"id": 10, "source_type": 3, "source_id": "501"},
                   "11": {"id": 11, "source_type": 3, "source_id": "502"}}
        identity = policy.material_identity([row], sources, self.scope)
        self.assertEqual(identity["material_ids"], ["501", "502"])
        self.assertFalse(identity["reason"])
        row["original_source_id"] = "501"
        self.assertEqual(policy.material_identity([row], sources, self.scope)["reason"], "source_original_conflict")

    def test_unresolved_or_wrong_type_source_cannot_fallback(self):
        row = lineage()
        row["source_id"] = "10"
        self.assertEqual(policy.material_identity([row], {}, self.scope)["reason"], "source_missing")
        self.assertEqual(policy.material_identity([row], {"10": {"source_type": 2, "source_id": "501"}}, self.scope)["reason"], "source_type_unverified")

    def test_source_capacity_is_smaller_than_ad_field(self):
        row = lineage()
        row["source_id"] = "10"
        result = policy.material_identity([row], {"10": {"source_type": 3, "source_id": "1" * 255}}, self.scope)
        self.assertEqual(result["reason"], "possibly_truncated_ids")

    def test_current_creative_filters_historical_lineage(self):
        old = lineage(material="700", creative="899")
        current = lineage()
        result = policy.material_identity([old, current], {}, self.scope, creative_id="900")
        self.assertEqual(result["material_ids"], ["501"])
        self.assertFalse(result["reason"])
        self.assertEqual(policy.material_identity([old], {}, self.scope, creative_id="900")["reason"], "current_creative_lineage_missing")

    def test_duplicate_ad_records_must_agree(self):
        for other in (lineage(material="502"), lineage(product="1723"), lineage(ad_id="101")):
            self.assertTrue(policy.material_identity([lineage(), other], {}, self.scope)["reason"])
        self.assertFalse(policy.material_identity([lineage(), lineage()], {}, self.scope)["reason"])

    def test_out_of_product_scope(self):
        self.assertEqual(self.identity(product="826")["reason"], "product_out_of_scope")

    def test_qualifying_policy_exact_keys_only(self):
        self.assertEqual(policy.qualifying_policies(json.dumps({"global": {"Spam": "", "Violence & Graphic Content": "x"}})), ["Violence & Graphic Content", "Spam"])
        for raw in ('[{"level":"AD","error_code":2490468}]', '{"global":{"spam":"Spam"}}', '{"error":"Spam"}', 'broken', None):
            self.assertEqual(policy.qualifying_policies(raw), [])

    def test_window_boundaries_thirty_days_and_duplicate_policy_hits(self):
        violations = [
            {"id": 1, "ad_id": "100", "created_at": "2026-09-23 09:00:00", "issues_info": {"global": {"Spam": "", "Human Exploitation": ""}}},
            {"id": 2, "ad_id": "100", "created_at": "2026-10-20 09:00:00", "issues_info": {"global": {"Spam": ""}}},
            {"id": 3, "ad_id": "101", "created_at": "2026-09-23 08:59:59", "issues_info": {"global": {"Spam": ""}}},
            {"id": 4, "ad_id": "102", "created_at": "2026-10-23 09:00:00", "issues_info": {"global": {"Spam": ""}}},
        ]
        rows = {aid: [lineage(ad_id=aid)] for aid in ("100", "101", "102")}
        result = policy.build_evidence(violations, rows, {}, self.scope, "2026-09-23T01:00:00Z", "2026-10-23T01:00:00Z")
        self.assertEqual(result["materials"]["501"]["count"], 1)
        self.assertEqual(result["materials"]["501"]["policies"], ["Human Exploitation", "Spam"])
        self.assertEqual([x["reason"] for x in result["excluded"]], ["outside_window", "outside_window"])

    def test_ambiguous_multimaterial_violation_is_not_distributed(self):
        violation = {"id": 1, "ad_id": "100", "source_id": 0, "created_at": "2026-10-22", "issues_info": {"global": {"Spam": ""}}}
        rows = {"100": [lineage(material="501,502")]}
        result = policy.build_evidence([violation], rows, {}, self.scope, "2026-09-23", "2026-10-24")
        self.assertFalse(result["materials"])
        self.assertEqual(result["excluded"][0]["reason"], "ambiguous_violation_material")

    def test_multimaterial_with_explicit_vio_source_counts_only_that_material(self):
        violation = {"id": 1, "ad_id": "100", "source_id": 10, "created_at": "2026-10-22", "issues_info": {"global": {"Spam": ""}}}
        sources = {"10": {"source_type": 3, "source_id": "502"}}
        result = policy.build_evidence([violation], {"100": [lineage(material="501,502")]}, sources, self.scope, "2026-09-23", "2026-10-24")
        self.assertEqual(set(result["materials"]), {"502"})
        self.assertEqual(result["materials"]["502"]["count"], 1)

    def test_conflicting_explicit_violation_identity_excluded(self):
        violation = {"id": 1, "ad_id": "100", "source_id": 10, "created_at": "2026-10-22", "issues_info": {"global": {"Spam": ""}}}
        result = policy.build_evidence([violation], {"100": [lineage()]}, {"10": {"source_type": 3, "source_id": "502"}}, self.scope, "2026-09-23", "2026-10-24")
        self.assertEqual(result["excluded"][0]["reason"], "violation_material_identity_conflict")

    def test_active_is_protected_even_with_many_violations(self):
        for count in (0, 4, 5, 20):
            result = policy.evaluate_ad(live_ad(), self.identity(), evidence(count))
            self.assertFalse(result["eligible"])
            self.assertEqual(result["reason"], "active_ad_protected")

    def test_paused_requires_five_distinct_violations(self):
        ad = live_ad()
        ad.update(status="PAUSED", effective_status="PAUSED")
        for count in (0, 1, 3, 4):
            self.assertEqual(policy.evaluate_ad(ad, self.identity(), evidence(count))["reason"], "violation_threshold_not_met")
        self.assertEqual(policy.evaluate_ad(ad, self.identity(), evidence(5))["rules"], ["paused_material_5"])

    def test_each_parent_pause_counts_as_closed(self):
        for parent, effective in (("campaign", "CAMPAIGN_PAUSED"), ("adset", "ADSET_PAUSED")):
            ad = live_ad()
            ad[parent]["status"] = "PAUSED"
            ad["effective_status"] = effective
            self.assertEqual(policy.evaluate_ad(ad, self.identity(), evidence())["rules"], ["paused_material_5"])

    def test_recovered_historical_violation_and_omitted_review_fields_allowed(self):
        ad = live_ad()
        ad.update(status="PAUSED", effective_status="PAUSED")
        self.assertTrue(policy.evaluate_ad(ad, self.identity(), evidence(5))["eligible"])
        ad.update(issues_info=[], ad_review_feedback={"global": {}, "placement_specific": {}})
        self.assertTrue(policy.evaluate_ad(ad, self.identity(), evidence(5))["eligible"])

    def test_current_review_issue_or_unreadable_feedback_skips(self):
        for field in ("issues_info", "ad_review_feedback"):
            ad = live_ad()
            ad[field] = {"global": {"Spam": ""}}
            self.assertEqual(policy.evaluate_ad(ad, self.identity(), evidence(4))["reason"], "current_review_issue")
            ad[field] = "invalid json"
            self.assertEqual(policy.evaluate_ad(ad, self.identity(), evidence(4))["reason"], "review_feedback_unreadable")

    def test_statuses_and_parent_contradictions_skip(self):
        for status in ("DISAPPROVED", "PENDING_REVIEW", "IN_PROCESS", "DELETED", "ARCHIVED", "", "SOMETHING_NEW"):
            ad = live_ad()
            ad["effective_status"] = status
            self.assertFalse(policy.evaluate_ad(ad, self.identity(), evidence(4))["eligible"])
        ad = live_ad()
        ad["campaign"]["status"] = "PAUSED"
        self.assertEqual(policy.evaluate_ad(ad, self.identity(), evidence(4))["reason"], "current_status_conflict")
        ad = live_ad()
        ad["effective_status"] = "ADSET_PAUSED"
        self.assertEqual(policy.evaluate_ad(ad, self.identity(), evidence(4))["reason"], "current_status_conflict")

    def test_current_ad_with_hit_and_good_material_can_be_deleted(self):
        identity = self.identity(material="501,502")
        ad = live_ad(); ad.update(status="PAUSED", effective_status="PAUSED")
        result = policy.evaluate_ad(ad, identity, evidence(5, mid="502"))
        self.assertTrue(result["eligible"])
        self.assertEqual(result["material_ids"], ["501", "502"])

    def test_pause_gate_also_applies_to_shared_ad_candidates(self):
        ad = live_ad()
        ad["shared_outside_scope"] = True
        self.assertFalse(policy.evaluate_ad(ad, self.identity(), evidence(5))["eligible"])
        ad.update(status="PAUSED", effective_status="PAUSED")
        self.assertTrue(policy.evaluate_ad(ad, self.identity(), evidence(5))["eligible"])

    def test_lookup_gate_validates_distinct_ids_and_threshold(self):
        self.assertEqual(policy.qualified_material_ids(evidence(4)), set())
        self.assertEqual(policy.qualified_material_ids(evidence(5)), {"501"})
        proof = evidence(5)
        proof['materials']['501']['violating_ad_ids'][-1] = '200'
        with self.assertRaisesRegex(ValueError, 'evidence_count_conflict'):
            policy.qualified_material_ids(proof)
        self.assertEqual(policy.POLICY_VERSION, '20261027-v3')
        self.assertFalse(policy.POLICY_CONTRACT['allow_active'])

    def test_evidence_count_and_current_identity_cannot_conflict(self):
        proof = evidence(3)
        proof["materials"]["501"]["count"] = 4
        self.assertEqual(policy.evaluate_ad(live_ad(), self.identity(), proof)["reason"], "evidence_count_conflict")
        self.assertFalse(policy.evaluate_ad(live_ad(creative="901"), self.identity(), evidence(4))["eligible"])


if __name__ == "__main__":
    unittest.main()
