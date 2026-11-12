"""Read-only hit-drama Page preview; --selection additionally checks materials."""
import argparse
from collections import Counter
from contextlib import contextmanager
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import random
import sys
import time

SOURCE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SOURCE))

from scripts.preview_fb_profile_selection import ReadOnlyStore, live_environment, source_reader
from features.fb_auto_posts.core import utc_iso
from features.fb_auto_posts.effects import choose_material, load_scores
from features.fb_auto_posts.hit_profile import filter_pages, freeze_match, profileaudit
from features.fb_auto_posts.languages import page_language
from features.fb_auto_posts.repositories import MaterialRepository, PagePoolRepository
from features.fb_auto_posts.strategy import cooldown_content_ids, in_slot
from features.fb_video_publish.guards import automatic_block, manual_block
from features.hit_drama_target import resolve_target


@contextmanager
def profile_environment(env):
    """Use only the live profile path; never copy credentials into our output."""
    key = "FB_AUTO_PROFILE_ROOT"
    previous = os.environ.get(key)
    os.environ[key] = env.get(key, "/mnt/data-disk/fb-page-portraits")
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = previous


def read_request(ledger, args):
    with ledger.connect() as conn:
        if args.run_id:
            row = conn.execute(
                "SELECT r.template_id,r.template_version,r.config_json,r.status run_status,"
                "r.queued_tasks,r.skipped_tasks,t.scope_is_admin,t.owner_user_id,"
                "t.status template_status,t.current_version "
                "FROM fb_auto_run r JOIN fb_auto_template t ON t.id=r.template_id WHERE r.id=?",
                (args.run_id,),
            ).fetchone()
            if row is None:
                raise ValueError("preview_run_not_found")
            template = dict(row)
            config = json.loads(template.pop("config_json"))
            marker = config.pop("hit_drama_publish", {})
            request = marker.get("request")
            if not isinstance(request, dict) or not {"drama_id", "id_type"}.issubset(request):
                raise ValueError("preview_run_not_hit_drama")
        else:
            row = conn.execute(
                "SELECT t.id template_id,t.current_version template_version,t.current_version,"
                "t.scope_is_admin,t.owner_user_id,t.status template_status,v.config_json "
                "FROM fb_auto_template t JOIN fb_auto_template_version v "
                "ON v.template_id=t.id AND v.version=t.current_version WHERE t.id=?",
                (args.template_id,),
            ).fetchone()
            if row is None:
                raise ValueError("preview_template_not_found")
            template = dict(row)
            config = json.loads(template.pop("config_json"))
            config.pop("hit_drama_publish", None)
            request = dict(drama_id=args.drama_id, id_type=args.id_type)
    return template, config, request


def brief_audit(audit):
    return {key: audit[key] for key in (
        "page_id", "content_id", "language", "matched", "match_policy",
        "matched_audience", "matched_themes", "profile_version",
        "profile_generated_at", "source_window", "profile_stale",
    ) if key in audit}


class CountedReader:
    """Count source records returned during optional candidate reads only."""
    def __init__(self, source):
        self.source = source
        self.material_rows = 0
        self.material_ids = set()

    def __getattr__(self, key):
        return getattr(self.source, key)

    def select(self, sql, params=()):
        rows = self.source.select(sql, params)
        if "ads_custom_source" in sql:
            ids = [str(row["material_id"]) for row in rows if row.get("material_id") is not None]
            self.material_rows += len(ids)
            self.material_ids.update(ids)
        return rows


def preview_selection(source, ledger, metrics, pages, config, frozen, identity, now):
    """Evaluate candidate selection against one read-only ledger snapshot."""
    began = time.monotonic()
    pages = filter_pages(pages, frozen)
    with ledger.connect() as conn:
        maximum = max((len(ledger._cooldown_material_ids(conn, p.page_id, int(config["cooldown_days"])))
                       for p in pages), default=0)
    limit = min(5000, max(1000 if config.get("drama_cooldown_hours") else 500, maximum + 100))
    counted = CountedReader(source)
    repository = MaterialRepository(counted, metrics, now_fn=lambda: now, rng=random.Random(0))
    snapshots, scans = {}, []
    for lang in sorted({page_language(p.language) for p in pages} - {""}):
        scan_began = time.monotonic()
        item = repository.candidate_snapshot(dict(config, language=lang,
            hit_drama_publish=frozen, _candidate_limit=limit, _profile_selection=False))
        snapshots[lang] = item
        scans.append(dict(language=lang, eligible_material_count=len(item.candidates),
                          eligible_drama_count=len({m.content_id for m in item.candidates}),
                          metric_dates=list(item.metric_dates), seconds=round(time.monotonic() - scan_began, 3)))
    reasons, examples, queue_languages = Counter(), [], Counter()
    slot = identity[-1]
    now_text = utc_iso(now)
    with ledger.connect() as conn:
        conn.execute("BEGIN")
        scores = load_scores(conn, config, now)
        for page in pages:
            lang = page_language(page.language)
            reason = automatic_block(conn, page.page_id) or manual_block(conn, page.page_id)
            if not reason and not in_slot(config, page.page_id, slot, manual=True):
                reason = "fb_auto_page_frequency_limit"
            if not reason and page.eligible_token_count <= 0:
                reason = "fb_page_missing_eligible_token"
            material = None
            if not reason:
                excluded = ledger._cooldown_material_ids(conn, page.page_id, int(config["cooldown_days"]))
                if limit < 5000 and len(excluded) > limit - 50:
                    reason = "fb_auto_candidate_reservations_changed"
                else:
                    blocked = cooldown_content_ids(conn, page.page_id, now_text,
                                                   int(config.get("drama_cooldown_hours", 0)), now_text)
                    allowed = set(frozen["page_content_ids"][page.page_id])
                    scoped = [m for m in snapshots[lang].candidates
                              if m.content_id in allowed and page_language(m.language) == lang]
                    candidates = [m for m in scoped if m.content_id not in blocked]
                    material, _ = choose_material(candidates, excluded, materials=repository,
                        config=config, page_id=page.page_id, slot_key=slot, scores=scores)
                    if material is None:
                        reason = ("fb_auto_no_video_after_drama_cooldown" if scoped and not candidates else
                                  "fb_auto_no_video_after_material_cooldown" if candidates and all(m.material_id in excluded for m in candidates) else
                                  "fb_auto_no_eligible_video")
            if reason:
                reasons[reason] += 1
                continue
            queue_languages[lang] += 1
            if len(examples) < 10:
                audit = brief_audit(profileaudit(page.page_id, material.content_id, frozen))
                examples.append(dict(audit, material_id=material.material_id))
    return dict(queue_candidate_pages=sum(queue_languages.values()),
                queue_candidate_languages=dict(sorted(queue_languages.items())),
                skipped_pages=sum(reasons.values()), skip_reasons=dict(sorted(reasons.items())),
                scanned_material_rows=counted.material_rows, scanned_unique_materials=len(counted.material_ids),
                eligible_material_count=sum(len(s.candidates) for s in snapshots.values()),
                candidate_limit=limit, scans=scans, examples=examples,
                seconds=round(time.monotonic() - began, 3))


def preview(args):
    began = time.monotonic()
    now = datetime.now(timezone.utc)
    env = live_environment()
    source = source_reader(env)
    ledger = ReadOnlyStore(env.get("FB_AUTO_POST_DB_PATH", "/mnt/data-disk/fb-auto-post-publisher/fb-auto-post.sqlite3"), now)
    template, config, request = read_request(ledger, args)
    pages = PagePoolRepository(source).list_pages(config["group_ids"],
        is_admin=bool(template["scope_is_admin"]), owner_user_id=str(template["owner_user_id"]))
    target = resolve_target(source.select, source.schema, config["app_id"], request)
    identity = [template["template_id"], template["template_version"], "read-only-preview:" + utc_iso(now)]
    with profile_environment(env):
        frozen = freeze_match(pages, target, source, identity, app_id=config["app_id"])
    matched = filter_pages(pages, frozen)
    examples = []
    for page in matched:
        for cid in frozen["page_content_ids"][page.page_id]:
            if len(examples) < 10:
                examples.append(brief_audit(profileaudit(page.page_id, cid, frozen)))
    result = dict(read_only=True, ok=True, preview_at=utc_iso(now),
        source_run_id=args.run_id, template_id=template["template_id"],
        template_version=template["template_version"], current_template_version=template["current_version"],
        template_status=template["template_status"], request={key: request[key] for key in ("drama_id", "id_type")},
        resolved_content_ids=target["content_ids"], resolved_languages=target["languages"],
        scoped_pages=len(pages), publishable_scope_pages=sum(p.eligible_token_count > 0 for p in pages),
        matched_pages=len(matched), matched_languages=dict(sorted(Counter(frozen["page_languages"][p.page_id] for p in matched).items())),
        skipped_pages=len(frozen["skipped_pages"]), skip_reasons=dict(sorted(Counter(p["reason"] for p in frozen["skipped_pages"]).items())),
        match_policy=frozen["match_policy"], selection_requested=args.selection, examples=examples)
    if args.run_id:
        result["existing_run"] = {key: template[key] for key in ("run_status", "queued_tasks", "skipped_tasks")}
    if args.selection and matched:
        metrics = ReadOnlyStore(env.get("FB_AUTO_METRIC_DB_PATH", "/mnt/data-disk/fb-auto-post-publisher/fb-auto-metric.sqlite3"), now)
        result["selection"] = preview_selection(source, ledger, metrics, pages, config, frozen, identity, now)
        result["examples"] = result["selection"].pop("examples")
    elif args.selection:
        result["selection"] = dict(queue_candidate_pages=0, scanned_material_rows=0,
                                   scanned_unique_materials=0, eligible_material_count=0, reason="no_matching_page")
    result["elapsed_seconds"] = round(time.monotonic() - began, 3)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    scope = parser.add_mutually_exclusive_group(required=True)
    scope.add_argument("--run-id", type=int)
    scope.add_argument("--template-id", type=int)
    parser.add_argument("--drama-id")
    parser.add_argument("--id-type", choices=("content_id", "resource_id"))
    parser.add_argument("--selection", action="store_true", help="Also read eligible materials, cooldowns and reservations")
    args = parser.parse_args()
    if args.template_id and (not args.drama_id or not args.id_type):
        parser.error("--template-id requires --drama-id and --id-type")
    if args.run_id and (args.drama_id or args.id_type):
        parser.error("--run-id uses the saved run request; do not override its drama")
    try:
        result = preview(args)
    except Exception as error:
        # Database errors may contain SQL/connection details. Expose only a
        # typed failure and an existing business error code, never raw text.
        print(json.dumps(dict(read_only=True, ok=False, error_type=type(error).__name__,
                              error_code=getattr(error, "code", "preview_failed"))), flush=True)
        return 1
    print(json.dumps(result, ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
