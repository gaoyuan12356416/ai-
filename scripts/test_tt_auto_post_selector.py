#!/usr/bin/env python3
"""Offline tests for deterministic TT auto-post two-stage selection."""

from __future__ import annotations

import sys
import unittest
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from features.tt_auto_posts.repositories import (  # noqa: E402
    BlacklistSnapshot,
    DramaSourceRow,
    MaterialSourceRow,
    MetricWindowNotReady,
    MetricWindowRepository,
    complete_beijing_dates,
)
from features.tt_auto_posts.selector import (  # noqa: E402
    CandidateRejected,
    NoEligibleMaterial,
    SelectionError,
    SelectionRequest,
    SelectionRules,
    TwoStageSelector,
)


UTC = timezone.utc
NOW = datetime(2026, 8, 5, 10, 0, tzinfo=UTC)


def blacklist(*, dramas=(), materials=(), marker="a"):
    return BlacklistSnapshot(
        drama_series_codes=frozenset(dramas),
        material_data_source_ids=frozenset(materials),
        loaded_at_utc="2026-08-05T10:00:00Z",
        source_row_count=len(tuple(dramas)) + len(tuple(materials)),
        sha256=marker * 64,
    )


def drama(
    content_id,
    *,
    series_code=None,
    language="en",
    resource_type="1",
    deploy_time=1_754_300_000,
    app_id=1479,
    release_status=1,
    row_id=None,
):
    return DramaSourceRow(
        source_row_id=row_id or "row-%s" % content_id,
        content_id=str(content_id),
        series_code=series_code or "S-%s" % content_id,
        language=language,
        resource_type_v2=str(resource_type),
        deploy_time=deploy_time,
        app_id=app_id,
        release_status=release_status,
        name="Drama %s" % content_id,
        app="com.dramawave.app",
    )


def material(
    material_id,
    content_id,
    *,
    duration="30",
    language="en",
    product="Dramawave",
    material_type=2,
    is_delete=0,
    data_source=6,
):
    return MaterialSourceRow(
        material_id=str(material_id),
        content_id=str(content_id),
        language=language,
        product=product,
        material_type=material_type,
        is_delete=is_delete,
        media_url="https://media.example.test/%s.mp4" % material_id,
        material_name="material-%s" % material_id,
        video_duration=Decimal(str(duration)),
        data_source=data_source,
        tag_name="safe",
    )


def metric_row(metric_date, content_id, material_id, spend, revenue):
    return {
        "metric_date": metric_date,
        "platform": 0,
        "content_id": str(content_id),
        "material_id": str(material_id),
        "spend": str(spend),
        "af_revenue0": str(revenue),
    }


class FakeMetricStore:
    def __init__(self, rows=(), ready_dates=None):
        self.rows = list(rows)
        self.ready = set(
            complete_beijing_dates(NOW, 7)
            if ready_dates is None
            else ready_dates
        )
        self.loads = []

    def ready_metric_dates(self, platform, dates, *, product=None):
        self.loads.append(("ready", platform, tuple(dates), product))
        return self.ready.intersection(dates)

    def iter_ready_metric_rows(
        self,
        platform,
        dates,
        content_ids=None,
        *,
        product=None,
    ):
        self.loads.append(
            ("rows", platform, tuple(dates), tuple(content_ids or ()), product)
        )
        allowed_dates = set(dates)
        allowed_content = set(content_ids or ())
        return [
            row
            for row in self.rows
            if row["metric_date"] in allowed_dates
            and row["content_id"] in allowed_content
        ]


class FakeSource:
    def __init__(self, dramas=(), materials=None, blacklists=None):
        self.dramas = list(dramas)
        self.materials = dict(materials or {})
        self.blacklists = list(blacklists or [blacklist()])
        self.blacklist_calls = 0
        self.material_calls = []
        self.drama_call = None

    def blacklist_snapshot(self):
        index = min(self.blacklist_calls, len(self.blacklists) - 1)
        self.blacklist_calls += 1
        return self.blacklists[index]

    def list_drama_rows(
        self,
        *,
        language,
        now_epoch,
        deploy_since_epoch,
        resource_types,
    ):
        self.drama_call = {
            "language": language,
            "now_epoch": now_epoch,
            "deploy_since_epoch": deploy_since_epoch,
            "resource_types": tuple(resource_types),
        }
        return list(self.dramas)

    def list_material_rows(self, *, content_id, language):
        self.material_calls.append((content_id, language))
        return list(self.materials.get(content_id, []))


class FakeLegacyReader:
    def __init__(self, seen=(), seen_after_calls=None):
        self.seen = set(str(value) for value in seen)
        self.seen_after_calls = seen_after_calls
        self.calls = 0

    def seen_material_ids(self, material_ids):
        self.calls += 1
        values = self.seen
        if self.seen_after_calls and self.calls >= self.seen_after_calls[0]:
            values = values | {str(self.seen_after_calls[1])}
        return set(str(value) for value in material_ids).intersection(values)


class FakeStore:
    def __init__(self):
        self.existing = None
        self.reserved = set()
        self.cooldown = set()
        self.conflict_once = set()
        self.cooldown_conflict_once = set()
        self.reserve_calls = []

    def get_task_reservation(self, task_id):
        return self.existing

    def reserved_material_ids(self, material_ids):
        return set(material_ids).intersection(self.reserved)

    def cooldown_content_ids(self, *, template_id, content_ids, since_utc):
        return set(content_ids).intersection(self.cooldown)

    def reserve_material(self, **kwargs):
        self.reserve_calls.append(dict(kwargs))
        material_id = kwargs["material_id"]
        content_id = kwargs["content_id"]
        if content_id in self.cooldown_conflict_once:
            self.cooldown_conflict_once.remove(content_id)
            raise StoreConflict("tt_auto_drama_in_cooldown")
        if material_id in self.conflict_once:
            self.conflict_once.remove(material_id)
            raise StoreConflict("tt_auto_material_already_reserved")
        if material_id in self.reserved:
            return None
        self.reserved.add(material_id)
        return {
            "id": len(self.reserve_calls),
            "task_id": kwargs["task_id"],
            "material_id": material_id,
            "content_id": kwargs["content_id"],
            "status": "reserved",
        }


class StoreConflict(RuntimeError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


class FakeValidator:
    def __init__(self, rejected=(), overrides=None):
        self.rejected = set(str(value) for value in rejected)
        self.overrides = dict(overrides or {})
        self.calls = []

    def validate(self, material_id):
        self.calls.append(material_id)
        if material_id in self.rejected:
            raise CandidateRejected("unsafe", "unsafe candidate", 409)
        result = {
            "material_id": material_id,
            "content_id": "C%s" % (int(material_id) // 100),
            "material_language": "en",
            "description": "A recovery-safe drama description.",
            "drama_name": "Drama C%s" % (int(material_id) // 100),
            "material_tag": "romance",
            "source_media_url": "https://media.example.test/%s.mp4" % material_id,
        }
        result.update(self.overrides)
        return result


def rules(**overrides):
    raw = {
        "metric_window_days": 7,
        "platform": 0,
        "drama": {
            "sort": {"field": "spend", "direction": "desc"},
            "resource_types": ["1"],
            "launch_window_days": 0,
            "cooldown_days": 0,
        },
        "material": {
            "sort": {"field": "spend", "direction": "desc"},
        },
    }
    for key, value in overrides.items():
        if key in ("drama", "material"):
            raw[key] = {**raw[key], **value}
        else:
            raw[key] = value
    return SelectionRules.from_mapping(raw)


def request(rule=None, **overrides):
    values = {
        "run_id": 1,
        "task_id": 11,
        "template_id": 21,
        "template_version": 2,
        "account_id": "101",
        "language": "en",
        "rules": rule or rules(),
        "now": NOW,
    }
    values.update(overrides)
    return SelectionRequest(**values)


def selector(source, metric_store, legacy=None, store=None, validator=None):
    return TwoStageSelector(
        source,
        MetricWindowRepository(metric_store),
        legacy or FakeLegacyReader(),
        store or FakeStore(),
        material_validator=FakeValidator() if validator is None else validator,
    )


class RuleAndWindowTests(unittest.TestCase):
    def test_default_window_uses_seven_complete_beijing_days(self):
        self.assertEqual(
            complete_beijing_dates(NOW, 7),
            (
                "2026-07-29",
                "2026-07-30",
                "2026-07-31",
                "2026-08-01",
                "2026-08-02",
                "2026-08-03",
                "2026-08-04",
            ),
        )
        self.assertEqual(SelectionRules.from_mapping({}).metric_window_days, 7)

    def test_ranges_are_inclusive_and_invalid_ranges_fail(self):
        parsed = rules(
            drama={"spend": {"min": "10", "max": "10"}},
            material={"duration_seconds": {"min": "30", "max": "30"}},
        )
        self.assertTrue(parsed.drama.spend.contains(Decimal("10")))
        self.assertTrue(parsed.material.duration_seconds.contains(Decimal("30")))
        with self.assertRaises(ValueError):
            rules(drama={"spend": {"min": 11, "max": 10}})
        with self.assertRaises(ValueError):
            SelectionRules.from_mapping({"metric_window_days": True})
        with self.assertRaises(ValueError):
            SelectionRules.from_mapping({"platform": True})


class TwoStageSelectionTests(unittest.TestCase):
    def test_independent_rankings_and_first_drama_without_material_falls_through(self):
        dates = complete_beijing_dates(NOW, 7)
        metric_store = FakeMetricStore(
            [
                metric_row(dates[0], "C1", "101", 100, 300),
                metric_row(dates[0], "C2", "201", 50, 150),
                metric_row(dates[0], "C2", "202", 10, 1),
            ]
        )
        source = FakeSource(
            [drama("C1"), drama("C2")],
            {
                "C1": [],
                "C2": [material("201", "C2"), material("202", "C2")],
            },
        )
        selected = selector(source, metric_store).select_and_reserve(
            request(
                rules(
                    drama={"sort": {"field": "d0_roas", "direction": "desc"}},
                    material={"sort": {"field": "spend", "direction": "asc"}},
                )
            )
        )
        self.assertEqual(source.material_calls, [("C1", "en"), ("C2", "en")])
        self.assertEqual(selected.drama.content_id, "C2")
        self.assertEqual(selected.material.source.material_id, "202")

    def test_ratio_of_sums_drives_drama_and_material_filters(self):
        dates = complete_beijing_dates(NOW, 7)
        metric_store = FakeMetricStore(
            [
                metric_row(dates[0], "C1", "101", 10, 20),
                metric_row(dates[1], "C1", "101", 90, 0),
            ]
        )
        source = FakeSource(
            [drama("C1")],
            {"C1": [material("101", "C1")]},
        )
        selected = selector(source, metric_store).select_and_reserve(
            request(
                rules(
                    drama={"d0_roas": {"min": 20, "max": 20}},
                    material={"d0_roas": {"min": 20, "max": 20}},
                )
            )
        )
        self.assertEqual(selected.drama.metrics.d0_roas, Decimal("20"))
        self.assertEqual(selected.material.metrics.d0_roas, Decimal("20"))

    def test_zero_spend_roas_is_missing_and_always_sorted_last(self):
        dates = complete_beijing_dates(NOW, 7)
        rows = [metric_row(dates[0], "C2", "201", 10, 1)]
        for direction in ("asc", "desc"):
            with self.subTest(direction=direction):
                source = FakeSource(
                    [drama("C1"), drama("C2")],
                    {
                        "C1": [material("101", "C1")],
                        "C2": [material("201", "C2")],
                    },
                )
                selected = selector(source, FakeMetricStore(rows)).select_and_reserve(
                    request(
                        rules(
                            drama={
                                "sort": {
                                    "field": "d0_roas",
                                    "direction": direction,
                                }
                            }
                        )
                    )
                )
                self.assertEqual(selected.drama.content_id, "C2")
        with self.assertRaises(NoEligibleMaterial):
            selector(
                FakeSource(
                    [drama("C1")],
                    {"C1": [material("101", "C1")]},
                ),
                FakeMetricStore([]),
            ).select_and_reserve(
                request(rules(drama={"d0_roas": {"min": 0}}))
            )

    def test_stable_ties_use_content_and_numeric_material_identity(self):
        source = FakeSource(
            [drama("C2"), drama("C1")],
            {
                "C1": [material("110", "C1"), material("102", "C1")],
                "C2": [material("201", "C2")],
            },
        )
        selected = selector(source, FakeMetricStore([])).select_and_reserve(request())
        self.assertEqual(selected.drama.content_id, "C1")
        self.assertEqual(selected.material.source.material_id, "102")

    def test_duplicate_material_identity_is_fully_excluded(self):
        source = FakeSource(
            [drama("C1")],
            {
                "C1": [
                    material("101", "C1"),
                    material("101", "C1", duration="31"),
                    material("102", "C1"),
                ]
            },
        )
        selected = selector(source, FakeMetricStore([])).select_and_reserve(request())
        self.assertEqual(selected.material.source.material_id, "102")

    def test_hard_gates_language_release_deploy_type_and_duration(self):
        source = FakeSource(
            [
                drama("C0", deploy_time=0),
                drama("C1", release_status=0),
                drama("C2", app_id=1),
                drama("C3", language="es"),
                drama("C4", resource_type="2"),
                drama("C5"),
            ],
            {
                "C5": [
                    material("501", "C5", duration="3601"),
                    material("502", "C5", duration="60"),
                ]
            },
        )
        selected = selector(source, FakeMetricStore([])).select_and_reserve(
            request(
                rules(
                    drama={"resource_types": ["1"]},
                    material={"duration_seconds": {"min": 60, "max": 60}},
                )
            )
        )
        self.assertEqual(selected.drama.content_id, "C5")
        self.assertEqual(selected.material.source.material_id, "502")

    def test_blacklist_types_do_not_cross_match(self):
        source = FakeSource(
            [drama("C1", series_code="SERIES")],
            {"C1": [material("101", "C1")]},
            [
                blacklist(dramas={"C1"}, materials={"101"}),
            ],
        )
        selected = selector(source, FakeMetricStore([])).select_and_reserve(request())
        self.assertEqual(selected.material.source.material_id, "101")

        blocked_drama = FakeSource(
            [drama("C1", series_code="SERIES")],
            {"C1": [material("101", "C1")]},
            [blacklist(dramas={"SERIES"})],
        )
        with self.assertRaises(NoEligibleMaterial):
            selector(blocked_drama, FakeMetricStore([])).select_and_reserve(request())

        blocked_material = FakeSource(
            [drama("C1", series_code="SERIES")],
            {"C1": [material("101", "C1")]},
            [blacklist(materials={"C1"})],
        )
        with self.assertRaises(NoEligibleMaterial):
            selector(blocked_material, FakeMetricStore([])).select_and_reserve(request())

    def test_cooldown_is_template_scoped_and_zero_disables_it(self):
        source = FakeSource(
            [drama("C1"), drama("C2")],
            {
                "C1": [material("101", "C1")],
                "C2": [material("201", "C2")],
            },
        )
        store = FakeStore()
        store.cooldown.add("C1")
        selected = selector(source, FakeMetricStore([]), store=store).select_and_reserve(
            request(rules(drama={"cooldown_days": 7}))
        )
        self.assertEqual(selected.drama.content_id, "C2")

        fresh_store = FakeStore()
        fresh_store.cooldown.add("C1")
        selected = selector(
            source,
            FakeMetricStore([]),
            store=fresh_store,
        ).select_and_reserve(request(rules(drama={"cooldown_days": 0})))
        self.assertEqual(selected.drama.content_id, "C1")
        self.assertIsNone(fresh_store.reserve_calls[0]["cooldown_since_utc"])

    def test_atomic_cooldown_race_falls_through_to_next_drama(self):
        source = FakeSource(
            [drama("C1"), drama("C2")],
            {
                "C1": [material("101", "C1")],
                "C2": [material("201", "C2")],
            },
        )
        store = FakeStore()
        store.cooldown_conflict_once.add("C1")
        selected = selector(
            source,
            FakeMetricStore([]),
            store=store,
        ).select_and_reserve(request(rules(drama={"cooldown_days": 7})))
        self.assertEqual(selected.drama.content_id, "C2")
        self.assertEqual(
            [item["content_id"] for item in store.reserve_calls],
            ["C1", "C2"],
        )

    def test_claim_token_is_forwarded_only_to_atomic_reservation(self):
        source = FakeSource(
            [drama("C1")],
            {"C1": [material("101", "C1")]},
        )
        store = FakeStore()
        selected = selector(
            source,
            FakeMetricStore([]),
            store=store,
        ).select_and_reserve(request(claim_token="opaque-worker-claim"))
        self.assertEqual(store.reserve_calls[0]["claim_token"], "opaque-worker-claim")
        self.assertNotIn("claim_token", store.reserve_calls[0]["selection_snapshot"])
        self.assertNotIn("claim_token", selected.as_dict())

    def test_legacy_auto_and_reservation_race_all_filter_before_return(self):
        source = FakeSource(
            [drama("C1")],
            {
                "C1": [
                    material("101", "C1"),
                    material("102", "C1"),
                    material("103", "C1"),
                    material("104", "C1"),
                ]
            },
        )
        legacy = FakeLegacyReader({"101"})
        store = FakeStore()
        store.reserved.add("102")
        store.conflict_once.add("103")
        selected = selector(
            source,
            FakeMetricStore([]),
            legacy=legacy,
            store=store,
        ).select_and_reserve(request())
        self.assertEqual(selected.material.source.material_id, "104")
        self.assertEqual([item["material_id"] for item in store.reserve_calls], ["103", "104"])

    def test_final_blacklist_refresh_skips_newly_blocked_drama(self):
        source = FakeSource(
            [drama("C1", series_code="S1"), drama("C2", series_code="S2")],
            {
                "C1": [material("101", "C1")],
                "C2": [material("201", "C2")],
            },
            [
                blacklist(marker="a"),
                blacklist(dramas={"S1"}, marker="b"),
                blacklist(marker="c"),
            ],
        )
        selected = selector(source, FakeMetricStore([])).select_and_reserve(request())
        self.assertEqual(selected.drama.content_id, "C2")
        self.assertEqual(selected.initial_blacklist_sha256, "a" * 64)
        self.assertEqual(selected.final_blacklist_sha256, "c" * 64)

    def test_strict_item_rejection_continues_but_identity_mismatch_fails(self):
        source = FakeSource(
            [drama("C1")],
            {"C1": [material("101", "C1"), material("102", "C1")]},
        )
        validator = FakeValidator({"101"})
        selected = selector(
            source,
            FakeMetricStore([]),
            validator=validator,
        ).select_and_reserve(request())
        self.assertEqual(validator.calls, ["101", "102"])
        self.assertEqual(selected.material.source.material_id, "102")

    def test_strict_public_metadata_is_frozen_and_sensitive_fields_are_omitted(self):
        source = FakeSource(
            [drama("C1")],
            {"C1": [material("101", "C1")]},
        )
        store = FakeStore()
        validator = FakeValidator(
            overrides={
                "description": "  Frozen description.  ",
                "drama_name": "  Frozen Drama  ",
                "material_tag": "  romance  ",
                "source_media_url": "https://cdn.example.test/source.mp4",
                "access_token": "must-never-be-persisted",
                "internal_debug": {"authorization": "secret"},
            }
        )
        selected = selector(
            source,
            FakeMetricStore([]),
            store=store,
            validator=validator,
        ).select_and_reserve(request())
        frozen = store.reserve_calls[0]["selection_snapshot"]["material"]
        self.assertEqual(frozen["description"], "Frozen description.")
        self.assertEqual(frozen["drama_name"], "Frozen Drama")
        self.assertEqual(frozen["material_tag"], "romance")
        self.assertEqual(
            frozen["source_media_url"],
            "https://cdn.example.test/source.mp4",
        )
        self.assertNotIn("access_token", frozen)
        self.assertNotIn("internal_debug", frozen)
        self.assertEqual(selected.as_dict()["material"], frozen)

    def test_invalid_strict_recovery_metadata_fails_closed(self):
        source = FakeSource(
            [drama("C1")],
            {"C1": [material("101", "C1")]},
        )
        for overrides in (
            {"source_media_url": "http://cdn.example.test/source.mp4"},
            {"description": "x" * 4097},
            {"material_tag": ""},
        ):
            with self.subTest(overrides=tuple(overrides)):
                store = FakeStore()
                with self.assertRaisesRegex(
                    SelectionError,
                    "strict material validator",
                ) as caught:
                    selector(
                        source,
                        FakeMetricStore([]),
                        store=store,
                        validator=FakeValidator(overrides=overrides),
                    ).select_and_reserve(request())
                self.assertEqual(
                    caught.exception.code,
                    "tt_auto_material_validator_invalid",
                )
                self.assertEqual(store.reserve_calls, [])

    def test_existing_task_reservation_short_circuits_all_sources(self):
        source = FakeSource([], {})
        store = FakeStore()
        store.existing = {"id": 9, "task_id": 11, "material_id": "101"}
        selected = selector(source, FakeMetricStore([]), store=store).select_and_reserve(request())
        self.assertTrue(selected.idempotent)
        self.assertEqual(selected.reservation["id"], 9)
        self.assertEqual(source.blacklist_calls, 0)

    def test_missing_ready_day_fails_closed_before_material_query(self):
        dates = complete_beijing_dates(NOW, 7)
        source = FakeSource(
            [drama("C1")],
            {"C1": [material("101", "C1")]},
        )
        metric_store = FakeMetricStore([], ready_dates=dates[:-1])
        with self.assertRaises(MetricWindowNotReady) as caught:
            selector(source, metric_store).select_and_reserve(request())
        self.assertEqual(caught.exception.missing_dates, (dates[-1],))
        self.assertEqual(source.material_calls, [])


if __name__ == "__main__":
    unittest.main()
