"""Adapters around the installed TT selection and Direct Post implementation."""

import logging
import re
import sqlite3
import os
from collections import OrderedDict
from dataclasses import replace
from copy import copy
from datetime import datetime, timedelta
from pathlib import Path
from . import pub_launch

from .pub_store import connection, iso, load, now


class PublicationError(ValueError):
    def __init__(self, message, status=400, code="tt_promotion_invalid"):
        super().__init__(message)
        self.status, self.code = status, code


def number(value, minimum, maximum, label):
    if type(value) is not int or not minimum <= value <= maximum:
        raise PublicationError(label + "超出范围")
    return value


def old_databases():
    return (
        os.environ.get(
            "TT_PROMOTION_OLD_AUTO_DB", "/mnt/data-disk/tt-auto-post-publisher/tt-auto-post.sqlite3"
        ),
        os.environ.get("TT_AUTO_POST_LEGACY_DB_PATH", "/mnt/data-disk/tt-post-publisher/tt-post.sqlite3"),
    )


class OldHistory:
    def __init__(self, legacy, old_path, app_path, omit=None, extra=()):
        self.legacy, self.old, self.app = legacy, old_path, app_path
        self.omit, self.extra = omit or "", set(extra)

    def seen_material_ids(self, ids):
        result = self.extra.intersection(ids)
        if not ids:
            return result
        with connection(self.old) as old, connection(self.app) as app:
            for offset in range(0, len(ids), 400):
                batch = list(ids[offset : offset + 400])
                marks = ",".join("?" for _ in batch)
                result.update(self.legacy.seen_material_ids(batch))
                result.update(
                    str(row[0])
                    for row in old.execute(
                        "SELECT material_id FROM tt_auto_material_ledger WHERE material_id IN ("
                        + marks
                        + ")",
                        batch,
                    )
                )
                result.update(
                    str(row[0])
                    for row in app.execute(
                        "SELECT material_id FROM ttp_slot "
                        "WHERE slot_key<>? AND material_id IN (" + marks + ")",
                        [self.omit] + batch,
                    )
                )
        return result


class MetricCache:
    """Borrow only native metric queries. The legacy ledger connection is read-only."""

    def __init__(self, path):
        self.path = path

    def _reader(self):
        return connection(self.path)

    def ready_metric_dates(self, *a, **kw):
        from features.tt_auto_posts.core import TTPostAutoStore

        return TTPostAutoStore.ready_metric_dates(self, *a, **kw)

    def iter_ready_metric_rows(self, platform, metric_dates, content_ids=None, *, product=None):
        from features.tt_auto_posts.core import TTPostAutoStore

        ids = list(content_ids) if content_ids is not None else [None]
        for i in range(0, len(ids), 400):
            yield from TTPostAutoStore.iter_ready_metric_rows(
                self,
                platform=platform,
                metric_dates=metric_dates,
                content_ids=None if ids == [None] else ids[i : i + 400],
                product=product,
            )


def conflicts(account, at):
    """Read scheduled slots and unresolved writes from both previous owners."""
    old, pool = old_databases()
    results = []
    acct = str(account)
    with connection(old) as d:
        for row in d.execute(
            """SELECT id,status,scheduled_at_utc FROM tt_auto_task WHERE account_id=?
            AND (status IN ('publishing','unknown','reconciling') OR unknown_outcome=1 OR
            (coalesce(publish_id,'')<>'' AND status NOT IN ('published','failed')) OR
            (status='retry_wait' AND claim_phase='reconcile') OR
            (status IN ('selecting','preparing') AND julianday(lease_expires_at_utc)>julianday('now')) OR
            (abs((julianday(scheduled_at_utc)-julianday(?))*86400)<60 AND
             status NOT IN ('failed','cancelled','canceled','no_candidate'))) ORDER BY id LIMIT 5""",
            (acct, at),
        ):
            results.append("旧自动任务 #%s (%s)" % (row[0], row[1]))
        if d.execute(
            """SELECT 1 FROM social_random_slot s JOIN tt_auto_template t
            ON s.source_key='tt_auto:'||t.id AND t.enabled=1
            WHERE s.target_id=? AND s.local_date=substr(datetime(?, '+8 hours'),1,10)
            AND abs((julianday(s.planned_at)-julianday(?))*86400)<60 LIMIT 1""",
            (acct, at, at),
        ).fetchone():
            results.append("旧自动模板随机时段")
        local = at_local(at)
        for row in d.execute("""SELECT v.config_json FROM tt_auto_template t JOIN tt_auto_template_version v
                ON t.id=v.template_id AND t.current_version=v.version WHERE t.enabled=1"""):
            cfg = load(row[0])
            schedule = cfg.get("schedule", {})
            if acct in map(str, cfg["account_ids"]) and local in schedule.get("times", []):
                results.append("旧自动模板固定时段")
    with connection(pool) as d:
        for row in d.execute(
            """SELECT id,status FROM tt_post_queue WHERE account_id=?
            AND (status IN ('publishing','reconciling','unknown') OR unknown_outcome=1 OR
            (coalesce(publish_id,'')<>'' AND status NOT IN ('published','failed')) OR
            (abs((julianday(scheduled_at_utc)-julianday(?))*86400)<60 AND
            status NOT IN ('failed','canceled','cancelled'))) ORDER BY id LIMIT 5""",
            (acct, at),
        ):
            results.append("旧素材池任务 #%s (%s)" % (row[0], row[1]))
        for row in d.execute(
            "SELECT id,status FROM tt_post_direct_test WHERE account_id=? AND (status IN ('publishing','reconciling','unknown') OR unknown_outcome=1 OR (publish_id<>'' AND status NOT IN ('published','failed'))) LIMIT 3",
            (acct,),
        ):
            results.append("旧立即测试 #%s (%s)" % (row[0], row[1]))
        row = d.execute(
            "SELECT publish_times_json,account_ids_json FROM tt_post_auto_publish_config WHERE enabled=1"
        ).fetchone()
        if row and acct in map(str, load(row[1])) and local in load(row[0]):
            results.append("旧素材池固定时段")
        if d.execute(
            """SELECT 1 FROM social_random_slot WHERE target_id=? AND local_date=substr(datetime(?, '+8 hours'),1,10)
                       AND abs((julianday(planned_at)-julianday(?))*86400)<60 LIMIT 1""",
            (acct, at, at),
        ).fetchone():
            results.append("旧素材池随机时段")
    return results


def at_local(at):
    from datetime import datetime
    from .pub_store import BEIJING

    return datetime.fromisoformat(at.replace("Z", "+00:00")).astimezone(BEIJING).strftime("%H:%M")


def make_selector(native, store, spec, channel, seed, blocked=(), slot_key="", effects=None):
    from features.tt_auto_posts.selector import TwoStageSelector
    from .profiles import rank_candidates

    native_selector = native.selector
    source = native_selector.source
    exact = str(spec.get("content_id") or "")
    content_ids = set(spec.get("content_ids") or [])
    resources = set(spec.get("resource_ids") or [])
    allowed = set(spec.get("material_ids") or [])
    if spec.get("material_id"):
        allowed = {str(spec["material_id"])}
    # Resolve operator-supplied identities before traversing the drama catalog.
    parents = set()
    if allowed:
        for chunk in [sorted(allowed)[i : i + 200] for i in range(0, len(allowed), 200)]:
            marks = ",".join("?" for _ in chunk).replace("?", "%s")
            sql = f"SELECT DISTINCT data_source_id FROM `{source.schema}`.ads_custom_source WHERE id IN ({marks})"
            parents.update(str(r["data_source_id"]) for r in source._stream_select(sql, chunk))
    asof = datetime.fromisoformat(spec["_catalog_at"]) if spec.get("_catalog_at") else now()
    launch = pub_launch.normalize(spec.get("drama_launch_window_days", 0))
    anchor = datetime.fromisoformat(spec["_launch_anchor"]) if spec.get("_launch_anchor") else asof
    earliest, latest = pub_launch.bounds(launch, anchor)
    cutoff = (now() if slot_key else asof) - timedelta(days=spec.get("material_window_days", 0))
    excluded = set(spec.get("cooling") or [])

    class ScopedSource:
        def __getattr__(self, name):
            return getattr(source, name)

        def list_drama_rows(self, **kwargs):
            if isinstance(launch, str):
                kwargs.update(now_epoch=latest, deploy_since_epoch=earliest)
            rows = source.list_drama_rows(**kwargs)
            # X hit promotion uses the case-sensitive business series_code across languages.
            # Keep every source row for a matched ID so native ambiguity checks still apply.
            targets = content_ids | {
                r.content_id
                for r in rows
                if r.series_code in resources and r.app_id == native_selector.app_id
            }
            return [
                r
                for r in rows
                if (not exact or r.content_id == exact)
                and (not isinstance(launch, str) or earliest <= r.deploy_time <= latest)
                and (not spec.get("_publish_ceiling") or r.deploy_time <= spec["_publish_ceiling"])
                and (not (content_ids or resources) or r.content_id in targets)
                and (not allowed or r.content_id in parents)
            ]

    class Selector(TwoStageSelector):
        def _eligible_dramas(self, request, rows, *args):
            if excluded:
                rows = [r for r in rows if r.content_id not in excluded]
            if isinstance(launch, str):
                # Only the hard gate consumes this clock. Metric dates, reservation and cooldown stay real.
                request = replace(request, now=datetime.fromtimestamp(latest, asof.tzinfo))
            candidates = super()._eligible_dramas(request, rows, *args)
            if effects is not None:
                candidates = effects.apply("drama", candidates, args[-1])
            self.ranking = {}
            if spec.get("model") != "income" or not candidates:
                return candidates
            dto = [dict(content_id=r.content_id, language=r.language) for r in candidates]
            ranked, reason = rank_candidates(dto, channel.get("profile") or {}, seed, source=source)
            self.ranking = reason
            lookup = {r.content_id: r for r in candidates}
            return [lookup[r["content_id"]] for r in ranked]

        def _validate_material(self, request, drama, material):
            # The native strict resolver also checks exact (product, drama, language) release time.
            # During a simulation project only that read-only resolver to this candidate's own release.
            resolver = getattr(self.material_validator, "resolver", None)
            if (
                drama.deploy_time > int(request.now.timestamp())
                and isinstance(launch, str)
                and resolver is not None
            ):
                from features.tt_auto_posts.selector import ResolverMaterialValidator

                scoped = copy(self)
                projected = copy(resolver)
                projected._now_fn = lambda: datetime.fromtimestamp(drama.deploy_time, asof.tzinfo)
                scoped.material_validator = ResolverMaterialValidator(projected)
                return super(Selector, scoped)._validate_material(request, drama, material)
            return super()._validate_material(request, drama, material)

        def _eligible_materials(self, request, drama, rows, *args):
            if allowed:
                rows = [r for r in rows if r.material_id in allowed]
            if spec.get("material_window_days") and rows:
                sql = f"""SELECT CAST(id AS CHAR) AS material_id FROM `{source.schema}`.ads_custom_source
                         WHERE data_source=%s AND data_source_id=%s AND created_at>=FROM_UNIXTIME(%s)"""
                recent = {
                    str(v["material_id"])
                    for v in source._stream_select(
                        sql, (source.material_data_source, drama.content_id, int(cutoff.timestamp()))
                    )
                }
                rows = [r for r in rows if r.material_id in recent]
            candidates = super()._eligible_materials(request, drama, rows, *args)
            if effects is not None:
                candidates = effects.apply("material", candidates, args[-1], drama.content_id)
            return candidates

    history = OldHistory(native.legacy_reader, old_databases()[0], native.store.db_path, slot_key, blocked)
    return Selector(
        ScopedSource(),
        native_selector.metrics,
        history,
        store,
        material_validator=native_selector.material_validator,
        product=native_selector.product,
        app_id=native_selector.app_id,
        material_data_source=native_selector.material_data_source,
    )


class SnapshotReads:
    """Reuse immutable source reads while simulating one snapshot. Never used by the executor."""

    def __init__(self, delegate, methods, budget=50000):
        self.delegate = delegate
        self.methods = set(methods)
        self.values = OrderedDict()
        self.budget = budget
        self.used = 0

    def __getattr__(self, name):
        fn = getattr(self.delegate, name)
        if name not in self.methods:
            return fn

        def cached(*args, **kwargs):
            key = (name, repr(args), repr(sorted(kwargs.items())))
            if key not in self.values:
                result = fn(*args, **kwargs)
                if name == "_stream_select":
                    result = tuple(result)
                units = (
                    len(result.by_material) + len(result.by_drama)
                    if name == "load"
                    else (len(result) if isinstance(result, (tuple, list, dict, set)) else 1)
                )
                units = max(units, 1)
                while self.values and (self.used + units > self.budget or len(self.values) >= 512):
                    _, (_, removed) = self.values.popitem(last=False)
                    self.used -= removed
                self.used += units
                self.values[key] = (result, units)
            self.values.move_to_end(key)
            return self.values[key][0]

        return cached


class Runtime:
    def __init__(self, path, env=None):
        from features.tt_auto_posts.service import build_service_from_env
        from features.tt_auto_posts.repositories import MetricWindowRepository

        path = str(Path(path).resolve())
        if any(Path(p).resolve() == Path(path) for p in old_databases()):
            raise PublicationError("新版与旧版账本路径冲突", 500)
        source = dict(os.environ if env is None else env)
        source.update(
            TT_AUTO_POST_DB_PATH=path,
            TT_AUTO_POST_STATE_ROOT=str(Path(path).parent),
            TT_AUTO_POST_EXECUTION_NOT_BEFORE_UTC="",
        )
        self.native = build_service_from_env(source)
        self.store = self.native.store
        self.native.selector.metrics = MetricWindowRepository(MetricCache(old_databases()[0]))
        self.executor = promotion_executor(self.native, path)

    def conflicts(self, account, at):
        return conflicts(account, at)

    def preview_session(self):
        runtime = copy(self)
        runtime._preview_effects = {}
        runtime.native = copy(self.native)
        s = runtime.native.selector = copy(self.native.selector)
        s.source = SnapshotReads(
            s.source, ("list_drama_rows", "list_material_rows", "blacklist_snapshot", "_stream_select")
        )
        s.metrics = SnapshotReads(s.metrics, ("load",), budget=250000)
        s.material_validator = SnapshotReads(s.material_validator, ("validate",))
        return runtime

    @staticmethod
    def settings(channel):
        raw = channel.get("account_settings") or {}
        flags = (
            "allow_comment",
            "allow_duet",
            "allow_stitch",
            "brand_content_toggle",
            "brand_organic_toggle",
            "is_aigc",
        )
        privacy = raw.get("privacy_level")
        if (
            raw.get("configured") is False
            or privacy
            not in {"PUBLIC_TO_EVERYONE", "MUTUAL_FOLLOW_FRIENDS", "FOLLOWER_OF_CREATOR", "SELF_ONLY"}
            or any(type(raw.get(k)) not in (bool, int) or raw[k] not in (0, 1) for k in flags)
        ):
            raise PublicationError("请先在新版频道管理设置观看范围、互动偏好和内容披露", 409)
        return dict(privacy_level=privacy, **{k: bool(raw[k]) for k in flags}), int(raw.get("version") or 0)

    def preview(self, template, channel, opts, seed, blocked):
        from features.tt_auto_posts.service import _PreviewStore
        from features.tt_auto_posts.publisher import selector_rules
        from features.tt_auto_posts.selector import SelectionRequest

        config = template.config
        scoped = {**config, **opts}
        from .social_effects import SocialRules
        from features.social_material_effects.window import bounds
        selected_at = datetime.fromisoformat(opts['_catalog_at']) if opts.get('_catalog_at') else now()

        # Keep every slot for the same template/language on one projection generation.
        cache = getattr(self, "_preview_effects", {})
        effect_key = (template.id, template.version, channel["drama_language"], bounds(config.get('metric_window_days',7),selected_at))
        if effect_key not in cache:
            cache[effect_key] = SocialRules(config, channel["drama_language"],now=selected_at)
        effects = cache[effect_key]
        selector = make_selector(
            self.native, _PreviewStore(self.store), scoped, channel, seed, blocked, effects=effects
        )
        rules = selector_rules(pub_launch.native_config(config))
        # Promotion reserves each account's drama cooldown atomically in its own slot ledger.
        rules = replace(rules, drama=replace(rules.drama, cooldown_days=0))
        req = SelectionRequest(
            run_id=1,
            task_id=1,
            template_id=template.id,
            template_version=template.version,
            account_id=str(channel["account_id"]),
            language=channel["drama_language"],
            rules=rules,
            now=selected_at,
        )
        raw = selector.select_and_reserve(req).as_dict()
        m = raw["material"]
        drama = raw["drama"]
        ranking = getattr(selector, "ranking", {})
        if scoped.get("model") == "income":
            from .profiles import rank_candidates

            _, ranking = rank_candidates(
                [dict(content_id=drama["content_id"], language=drama["language"])],
                channel.get("profile") or {},
                seed,
                source=self.native.selector.source,
            )
        return dict(
            content_id=drama["content_id"],
            material_id=str(m["material_id"]),
            drama_name=m.get("drama_name") or drama.get("name", ""),
            description=m.get("description", ""),
            material_name=m.get("material_name", ""),
            deploy_time=drama["deploy_time"],
            social_effects=effects.audit(drama["content_id"], m["material_id"]),
            drama_metrics=drama["metrics"],
            material_metrics=m["metrics"],
            metric_dates=raw["metric_dates"],
            decision=ranking,
            profile_version=(channel.get("profile") or {}).get("version", ""),
            reason=(ranking.get("reason") or ranking.get("fallback_reason") or "频道收入画像")
            if ranking
            else "模板规则排序",
        )

    def step(self):
        report = self.executor.execute_next("tt-promotion-worker")
        try:
            self.executor.collect_public_link()
        except Exception as e:
            logging.getLogger(__name__).warning("TT public receipt refresh deferred: %s", type(e).__name__)
        return report


class ReceiptGPU:
    """Retain public facts returned by the credential-bound GPU endpoints."""

    def __init__(self, delegate, path, task):
        self.delegate, self.path, self.task = delegate, path, task

    def __getattr__(self, name):
        return getattr(self.delegate, name)

    def creator_info(self, **args):
        result = self.delegate.creator_info(**args)
        if (
            result.get("source_account_id") == args["source_account_id"]
            and result.get("job_id") == args["job_id"]
            and args["source_account_id"] == self.task.account_id
        ):
            handle = str((result.get("creator_info") or {}).get("creator_username") or "").lstrip("@")
            if re.fullmatch(r"[A-Za-z0-9._]{1,50}", handle):
                try:
                    with connection(self.path, write=True) as db:
                        db.execute(
                            """INSERT INTO ttp_public_receipt(task_id,account_id,creator_username)
                                 VALUES(?,?,?) ON CONFLICT(task_id) DO UPDATE SET creator_username=excluded.creator_username
                                 WHERE account_id=excluded.account_id""",
                            (self.task.id, self.task.account_id, handle),
                        )
                except sqlite3.Error:
                    logging.getLogger(__name__).warning("TT creator handle persistence deferred")
        return result

    def publish(self, **args):
        # Check participation immediately before the only external write. Existing
        # receipts can still be reconciled even if the operator pauses afterwards.
        from features.tt_auto_posts.publisher import AutoPostExecutionError

        with connection(self.path) as db:
            policy = db.execute(
                """SELECT c.enabled FROM ttp_slot s JOIN ttp_job j ON s.job_id=j.id
                LEFT JOIN promotion_channel_setting c ON c.tenant=j.tenant AND c.account_id=s.account_id
                WHERE s.native_run_id=? AND s.account_id=?""",
                (self.task.run_id, self.task.account_id),
            ).fetchone()
        if not policy or policy["enabled"] == 0:
            raise AutoPostExecutionError("tt_promotion_channel_paused", "频道已在新版暂停参与", 409)
        return self.delegate.publish(**args)

    def reconcile(self, **args):
        result = self.delegate.reconcile(**args)
        if (
            not isinstance(result, dict)
            or result.get("job_id") != args["job_id"]
            or str(result.get("publish_id") or "") != str(self.task.publish_id or "")
            or not self.task.publish_id
            or args["source_account_id"] != self.task.account_id
            or result.get("state") != "published"
            or self.task.account_settings.get("privacy_level") != "PUBLIC_TO_EVERYONE"
        ):
            return result
        status = result.get("status")
        ids = status.get("post_ids") if isinstance(status, dict) else None
        if not isinstance(ids, list):
            return result
        ids = list(dict.fromkeys(str(v) for v in ids if re.fullmatch(r"[1-9][0-9]{0,19}", str(v))))
        if not ids:
            return result
        from .pub_store import dump

        try:
            with connection(self.path, write=True) as db:
                db.execute(
                    """INSERT INTO ttp_public_receipt(task_id,account_id,publish_id,video_ids) VALUES(?,?,?,?)
                    ON CONFLICT(task_id) DO UPDATE SET publish_id=excluded.publish_id,video_ids=excluded.video_ids
                    WHERE account_id=excluded.account_id AND (publish_id='' OR publish_id=excluded.publish_id)""",
                    (self.task.id, self.task.account_id, self.task.publish_id, dump(ids)),
                )
                row = db.execute(
                    "SELECT creator_username FROM ttp_public_receipt WHERE task_id=? AND account_id=? AND publish_id=?",
                    (self.task.id, self.task.account_id, self.task.publish_id),
                ).fetchone()
                if len(ids) == 1 and row and row[0]:
                    result = dict(result, publish_url=f"https://www.tiktok.com/@{row[0]}/video/{ids[0]}")
        except sqlite3.Error:
            logging.getLogger(__name__).warning("TT public video receipt persistence deferred")
        return result


def promotion_executor(native, path):
    from features.tt_auto_posts.publisher import (
        AutoPostExecutor,
        _ClaimedSelectionStore,
        AutoPostExecutionError,
    )
    from features.tt_auto_posts.links import (
        build_auto_w2a_url,
        build_auto_short_url,
        write_auto_short_redirect,
        render_auto_caption,
    )
    from datetime import datetime

    class Executor(AutoPostExecutor):
        def _select(self, task, claim_token):
            # Native selection includes credential, duration and creator checks that differ by release.
            # Project its per-call template without changing the shared executor or native module.
            phase = copy(self)
            phase.store = pub_launch.NativeSelectStore(self.store)
            return super(Executor, phase)._select(task, claim_token)

        def _video_route_for_task(self, task):
            route = super()._video_route_for_task(task)
            return replace(route, gpu_client=ReceiptGPU(route.gpu_client, path, task))

        def collect_public_link(self):
            stamp = now()
            with connection(path, write=True) as db:
                r = db.execute(
                    """SELECT t.id,t.account_id FROM tt_auto_task t LEFT JOIN ttp_public_receipt p ON p.task_id=t.id
                    WHERE t.status='published' AND t.publish_id<>'' AND t.unknown_outcome=0
                    AND json_extract(t.account_settings_json,'$.privacy_level')='PUBLIC_TO_EVERYONE'
                    AND t.publish_url='' AND t.published_at_utc>=? AND coalesce(p.next_check,'')<=?
                    AND coalesce(p.attempts,0)<8 ORDER BY coalesce(p.next_check,''),t.id LIMIT 1""",
                    (iso(stamp - timedelta(days=7)), iso(stamp)),
                ).fetchone()
                if not r:
                    return
                db.execute(
                    """INSERT INTO ttp_public_receipt(task_id,account_id,attempts,next_check) VALUES(?,?,1,?)
                    ON CONFLICT(task_id) DO UPDATE SET attempts=attempts+1,next_check=excluded.next_check""",
                    (r["id"], r["account_id"], iso(stamp + timedelta(minutes=30))),
                )
            task = self.store.get_task(r["id"])
            gpu = self._video_route_for_task(task).gpu_client
            with self.account_source.publish_credentials(task.account_id) as creds:
                response = gpu.reconcile(
                    job_id=task.gpu_job_id,
                    source_account_id=task.account_id,
                    access_token=creds.reveal_access_token(),
                )
            from features.tt_auto_posts.publisher import _public_tiktok_url

            link = _public_tiktok_url(response.get("publish_url"))
            if (
                link
                and response.get("publish_id") == task.publish_id
                and response.get("state") == "published"
            ):
                with connection(path, write=True) as db:
                    db.execute(
                        "UPDATE tt_auto_task SET publish_url=? WHERE id=? AND status='published' AND publish_id=? AND publish_url=''",
                        (link, task.id, task.publish_id),
                    )

        def frozen(self, task):
            with connection(path) as db:
                row = db.execute(
                    """SELECT s.*,c.caption_body,c.source,v.config_json FROM ttp_slot s
                    JOIN ttp_job j ON j.id=s.job_id JOIN tt_auto_template_version v
                    ON v.template_id=j.template_id AND v.version=j.version
                    JOIN ttp_copy c ON c.slot_key=s.slot_key WHERE s.native_run_id=?""",
                    (task.run_id,),
                ).fetchone()
                if not row:
                    raise AutoPostExecutionError("tt_promotion_snapshot_missing", "新版任务快照缺失", 409)
                return dict(row)

        def _phase_selector(self, claim_token):
            # The native store passes each claim into an atomic TT material reservation.
            class FrozenSelector:
                def select_and_reserve(inner, request):
                    task = self.store.get_task(request.task_id)
                    row = self.frozen(task)
                    spec = {**load(row["config_json"]), **load(row["opts"])}
                    choice = load(row["choice"])
                    spec.update(
                        content_id=choice["content_id"],
                        material_id=choice["material_id"],
                        model="rules",
                        _publish_ceiling=int(datetime.fromisoformat(row["scheduled_at"]).timestamp())
                        if choice.get("release_deferred")
                        else None,
                        material_window_days=load(row["config_json"]).get("material_window_days", 0),
                    )
                    request = replace(
                        request,
                        rules=replace(request.rules, drama=replace(request.rules.drama, cooldown_days=0)),
                    )
                    sel = make_selector(
                        native,
                        _ClaimedSelectionStore(self.store, claim_token),
                        spec,
                        {},
                        row["slot_key"],
                        slot_key=row["slot_key"],
                    )
                    return sel.select_and_reserve(request)

            return FrozenSelector()

        def _caption_and_link(self, task):
            row = self.frozen(task)
            choice = load(row["choice"])
            stamp = task.reserved_at_utc or task.created_at
            ts = int(datetime.fromisoformat(str(stamp).replace("Z", "+00:00")).timestamp())
            material = self._selection_material(task)
            drama_name = choice["drama_name"]
            url = build_auto_w2a_url(
                link_id=task.id,
                username=task.account_username or task.account_id,
                timestamp=ts,
                language=task.drama_language,
                drama_name=drama_name or task.content_id,
                tag=material.get("material_tag") or "TTauto",
                page_name=task.account_display_name or task.account_id,
                page_id=task.account_id,
                material_name=material.get("material_name") or task.material_id,
                material_id=task.material_id,
                content_id=task.content_id,
            )
            write_auto_short_redirect(self.short_link_root, task.id, url)
            code = self.code_route_broker.freeze_route(
                task.id, content_id=task.content_id, long_url=url, created_at=str(stamp)
            )
            tmpl = row["caption_body"] or load(row["config_json"])["caption_template"]

            def render(text):
                return render_auto_caption(
                    text,
                    task.content_id,
                    short_url=build_auto_short_url(task.id),
                    m_url=None,
                    description=choice.get("description") or drama_name,
                    code=code,
                    drama_name=drama_name,
                )

            try:
                caption = render(tmpl)
            except Exception:
                if task.caption or row["source"] != "ai":
                    raise
                original = load(row["config_json"])["caption_template"]
                caption = render(original)
                with connection(path, write=True) as db:
                    db.execute(
                        """UPDATE ttp_copy SET source='template',caption_body=?,reason='AI 成稿展开宏后无效'
                               WHERE slot_key=? AND source='ai'""",
                        (original, row["slot_key"]),
                    )
            if task.caption and task.caption != caption:
                raise AutoPostExecutionError("tt_promotion_caption_conflict", "任务文案与定稿不一致", 409)
            if choice.get("code") != code:
                with connection(path, write=True) as db:
                    db.execute(
                        "UPDATE ttp_slot SET choice=json_set(choice,'$.code',?) WHERE slot_key=?",
                        (code, row["slot_key"]),
                    )
            return caption

        def _sync_code_state(self, task, state, *, best_effort=False):
            try:
                self.code_route_broker.set_state(task.id, state=state, updated_at=iso(now()))
            except Exception:
                if not best_effort:
                    raise

        def _publish(self, task, claim_token=None):
            if (
                not task.publish_id
                and not task.unknown_outcome
                and task.status not in {"unknown", "reconciling"}
            ):
                row = self.frozen(task)
                choice = load(row["choice"])
                at = row["scheduled_at"]
                if choice.get("release_deferred"):
                    release = pub_launch.current_release(
                        native.selector.source, task.content_id, task.drama_language
                    )
                    if not release:
                        raise AutoPostExecutionError(
                            "tt_promotion_release_unavailable", "原剧目/语言没有可核验的上线时间", 409
                        )
                    due, release_at = pub_launch.times(at, release)
                    if due > iso(self._now()):
                        if due != at:
                            if conflicts(task.account_id, due):
                                raise AutoPostExecutionError(
                                    "tt_promotion_release_conflict", "调整后的上线时间与旧频道排期冲突", 409
                                )
                            with connection(path, write=True) as db:
                                if db.execute(
                                    """SELECT 1 FROM ttp_slot WHERE account_id=? AND slot_key<>?
                                    AND state NOT IN ('skipped','missed') AND abs(julianday(scheduled_at)-julianday(?))*86400<60 LIMIT 1""",
                                    (task.account_id, row["slot_key"], due),
                                ).fetchone():
                                    raise AutoPostExecutionError(
                                        "tt_promotion_release_conflict",
                                        "调整后的上线时间与新版频道排期冲突",
                                        409,
                                    )
                                db.execute(
                                    """UPDATE ttp_slot SET scheduled_at=?,choice=json_set(choice,'$.scheduled_at_utc',?,
                                    '$.release_at_utc',?,'$.deploy_time',?) WHERE slot_key=?""",
                                    (due, due, release_at, release, row["slot_key"]),
                                )
                        return self.store.transition_task(
                            task.id,
                            "retry_wait",
                            expected_statuses={task.status},
                            claim_token=claim_token,
                            updates=dict(
                                claim_phase="publish",
                                claim_worker="",
                                claim_token="",
                                lease_expires_at_utc="",
                                next_attempt_at_utc=due,
                                error_code="tt_promotion_wait_for_release",
                                error_message="等待原剧目/语言的有效发布时间",
                            ),
                            event_type="promotion_release_deferred",
                            message="原任务延至剧目上线",
                        )
                hits = conflicts(task.account_id, at)
                if hits or self.selector.legacy_reader.seen_material_ids([task.material_id]):
                    raise AutoPostExecutionError(
                        "tt_promotion_legacy_conflict", "原任务存在排期或素材冲突", 409
                    )
                with connection(old_databases()[0]) as db:
                    if db.execute(
                        "SELECT 1 FROM tt_auto_material_ledger WHERE material_id=?", (task.material_id,)
                    ).fetchone():
                        raise AutoPostExecutionError(
                            "tt_promotion_legacy_material", "素材已进入旧自动模板账本", 409
                        )
            return super()._publish(task, claim_token)

    executor = Executor.__new__(Executor)
    executor.__dict__.update(native.executor.__dict__)
    return executor
