"""Cumulative, measured organic effects applied to otherwise eligible TT candidates."""

from decimal import Decimal, InvalidOperation
import sqlite3

from features.social_material_effects import data, selection

METRICS = data.FILTERS
SOURCES = {"x", "facebook", "all", "tiktok"}
FIELDS = {f"x_{m}_{edge}" for m in METRICS for edge in ("min", "max")}
SORTS = {f"x_{m}" for m in METRICS}


class EffectSourceError(ValueError):
    pass


def normalized(rule):
    source = rule.get("effect_channel", "x")
    if not isinstance(source, str) or source not in SOURCES:
        raise ValueError("社媒效果来源无效")
    rule["effect_channel"] = source
    for metric in METRICS:
        low, high = (f"x_{metric}_{edge}" for edge in ("min", "max"))
        for field in (low, high):
            raw = rule.get(field)
            if raw in (None, ""):
                rule[field] = None
                continue
            try:
                value = Decimal(str(raw))
            except InvalidOperation:
                raise ValueError("社媒效果阈值必须是数值") from None
            if not value.is_finite() or value < 0 or value > 10**15:
                raise ValueError("社媒效果阈值超出范围")
            if metric != "revenue" and value != value.to_integral_value():
                raise ValueError("互动指标必须是非负整数")
            if metric == "revenue" and value * 100 != (value * 100).to_integral_value():
                raise ValueError("社媒收入请以美元为单位，保留至美分")
            rule[field] = float(value) if metric == "revenue" else int(value)
        if rule[low] is not None and rule[high] is not None and rule[low] > rule[high]:
            raise ValueError("社媒效果下限超过上限")
        if (
            source == "tiktok"
            and metric != "revenue"
            and (rule[low] is not None or rule[high] is not None or rule["sort_by"] == "x_" + metric)
        ):
            raise ValueError("TT 自身数据目前仅提供已归属收入")
    return rule


class SocialRules:
    def __init__(self, config, language):
        self.levels = {}
        active = {
            level: config.get(level + "_rule", {})
            for level in ("drama", "material")
            if selection.active(config.get(level + "_rule", {}))
        }
        if not active:
            return
        from .profiles import social_metrics

        native = None
        try:
            for attempt in range(2):
                versions = {}
                for level, rule in active.items():
                    source = rule.get("effect_channel", "x")
                    if source == "tiktok":
                        if native is None:
                            native = social_metrics(config.get("creator") or {}, language)
                            if native["status"] not in ("complete", "partial"):
                                raise EffectSourceError("TT 已归属收入快照尚未就绪")
                        index = native[level + "s"]
                        revision, stamp = {"tiktok": native["version"]}, native["end"]
                    else:
                        index, revision, stamp = selection.load(level, source, language, ["dramawave"])
                    if any(k in versions and versions[k] != v for k, v in revision.items()):
                        break
                    versions.update(revision)
                    self.levels[level] = (rule, source, index, revision, stamp)
                else:
                    return
            raise EffectSourceError("社媒效果快照正在切换，请重新预演")
        except (OSError, sqlite3.Error, KeyError) as exc:
            raise EffectSourceError("所选社媒效果快照暂不可用") from exc
        finally:
            self.native_partial = bool(native and native["status"] == "partial")

    def facts(self, level, content, material=None):
        rule, source, index, revision, stamp = self.levels[level]
        if source == "tiktok":
            cents = index.get(str(content if level == "drama" else material))
            return {
                "revenue_cents": None if self.native_partial else cents,
                "known": {"revenue_cents": cents},
                "coverage": {},
            }
        key = ("dramawave", str(content))
        if level == "material":
            key += ("ads_custom_source", str(material))
        return selection.evidence(index.get(key))

    def apply(self, level, candidates, rejected, content=None):
        if level not in self.levels:
            return candidates
        rule = self.levels[level][0]
        limits = []
        for metric in METRICS:
            low, high = (rule.get(f"x_{metric}_{s}") for s in ("min", "max"))
            if low is not None or high is not None:
                scale = 100 if metric == "revenue" else 1
                limits.append(
                    (
                        "revenue_cents" if scale == 100 else metric,
                        Decimal(str(low)) * scale if low is not None else None,
                        Decimal(str(high)) * scale if high is not None else None,
                    )
                )
        sort = rule.get("sort_by", "")
        sort_key = ("revenue_cents" if sort == "x_revenue" else sort[2:]) if sort in SORTS else None
        kept = []
        for row in candidates:
            cid = row.content_id if level == "drama" else content
            mid = None if level == "drama" else str(row.source.material_id)
            facts = self.facts(level, cid, mid)
            observed = facts.get("known", {})
            for key, low, high in limits:
                known = observed.get(key)
                total = facts.get(key)
                # As on X, a measured subtotal can prove a lower bound only.
                if (
                    known is None
                    or (low is not None and known < low)
                    or (high is not None and (total is None or total > high))
                ):
                    reason = level + "_social_" + key
                    rejected[reason] = rejected.get(reason, 0) + 1
                    break
            else:
                kept.append((row, str(mid or cid), observed.get(sort_key)))
        if sort_key:
            direction = -1 if rule.get("sort_direction") == "desc" else 1
            kept.sort(key=lambda item: (item[2] is None, direction * (item[2] or 0), item[1]))
        return [entry[0] for entry in kept]

    def audit(self, content, material):
        out = {}
        for level, (_, source, index, revision, stamp) in self.levels.items():
            facts = self.facts(level, content, material)

            def public(values):
                result = {m: values.get(m) for m in METRICS}
                cents = values.get("revenue_cents")
                result["revenue"] = cents / 100 if cents is not None else None
                return result

            coverage = facts.get("coverage", {}).copy()
            if "revenue_cents" in coverage:
                coverage["revenue"] = coverage.pop("revenue_cents")
            out[level] = dict(
                channel=source,
                versions=revision,
                as_of=stamp,
                posts=facts.get("post_count"),
                metrics=public(facts),
                known=public(facts.get("known", {})),
                coverage=coverage,
            )
        return out
