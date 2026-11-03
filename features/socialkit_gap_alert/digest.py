"""One daily Feishu summary with a self-contained HTML report."""
import argparse
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from html import escape
import json
import os
from pathlib import Path
import subprocess
import sys
from urllib.parse import urlparse

from features.post_daily_report.delivery import DeliveryStore, DefiniteFailure, send_card
from features.socialkit_analytics.refresh import ensure_data_mount
from features.socialkit_analytics.storage import atomic_json
from .cards import LABELS, beijing_time, dollars, payload_size, MAX_BYTES
from .collector import collect, comparison_period, safe_error
from .runner import CHAT_ID, DEFAULT_ROOT, DEFAULT_SOCIALKIT_ROOT, DEFAULT_FEISHU_CONFIG, run_lock, settings

BEIJING = timezone(timedelta(hours=8))
REASONS = {"no_confirmed_publication": "历史未查到成功发布", "large_revenue_gap": "收入差距较大"}


def reasons(event):
    return event.get("reasons") or [event["state"]]


def totals(events):
    return dict(count=len(events), dramas=len({e["drama"] for e in events}),
                socialkit=sum(e["socialkit_revenue_cents"] for e in events),
                own=sum(e["own_revenue_cents"] for e in events),
                unpublished=sum("no_confirmed_publication" in reasons(e) for e in events),
                gap=sum("large_revenue_gap" in reasons(e) for e in events))


def summary_card(report, url):
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.netloc:
        raise ValueError("html_url_must_be_https")
    events = report["events"]
    sums = totals(events)
    period = report.get("period") or comparison_period(report["date"])
    lines = ["Dramawave · %s 至 %s UTC" % (period["start"], period["end"]),
             "共 %d 项（平台×剧目），涉及 %d 部剧；待核实 %d 项。" %
             (sums["count"], sums["dramas"], len(report["pending"])),
             "历史未查到成功发布 %d 项；收入差距较大 %d 项（两类可重叠）。" % (sums["unpublished"], sums["gap"])]
    for platform, label in LABELS.items():
        group = totals([e for e in events if e["platform"] == platform])
        lines.append("%s：%d 项｜SocialKit %s / 我方 %s" % (label, group["count"], dollars(group["socialkit"]), dollars(group["own"])))
    lines += ["以上仅汇总命中项目；USD 原始收入，不扣退款。",
              "采集时间（北京）：%s；每天 11:30 汇总一次。" % beijing_time(report["collected_at"])]
    if report["errors"]:
        lines.append("部分平台采集异常，详见 HTML；缺失数据未当作零。")
    card = {"config": {"wide_screen_mode": True},
            "header": {"template": "orange", "title": {"tag": "plain_text", "content": "SocialKit 收入差异日报 · %d 项" % sums["count"]}},
            "elements": [{"tag": "div", "text": {"tag": "plain_text", "content": "\n".join(lines)}},
                         {"tag": "action", "actions": [{"tag": "button", "type": "primary", "text": {"tag": "plain_text", "content": "查看完整 HTML 明细"}, "url": url}]}]}
    if payload_size(card, CHAT_ID) > MAX_BYTES:
        raise ValueError("summary_exceeds_card_limit")
    return card


def render_html(report):
    esc = lambda value: escape(str(value), quote=True)
    events = sorted(report["events"], key=lambda e: (-(e["socialkit_revenue_cents"] - e["own_revenue_cents"]), e["platform"], e["drama"]))
    sums = totals(events)
    period = report.get("period") or comparison_period(report["date"])
    rows = []
    for event in events:
        sk, own = event["socialkit_revenue_cents"], event["own_revenue_cents"]
        ratio = format(Decimal(own) * 100 / Decimal(sk), ".2f") + "%" if sk else "—"
        why = reasons(event)
        cells = [LABELS[event["platform"]], event["drama_name"], event["drama"],
                 " / ".join(REASONS.get(r, r) for r in why), dollars(sk), dollars(own), dollars(sk-own), ratio,
                 event["history_success_count"], event.get("last_published_date") or "无"]
        rows.append('<tr data-platform="%s" data-reasons="%s" data-gap="%d" data-sk="%d">%s</tr>' %
                    (esc(event["platform"]), esc(" ".join(why)), sk-own, sk,
                     "".join('<td%s>%s</td>' % (' class="number"' if 4 <= i <= 8 else '', esc(v)) for i, v in enumerate(cells))))
    platform_rows = []
    for platform, label in LABELS.items():
        part = totals([e for e in events if e["platform"] == platform])
        platform_rows.append('<tr><td>%s</td><td>%d</td><td>%s</td><td>%s</td><td>%s</td></tr>' %
                             (label, part["count"], dollars(part["socialkit"]), dollars(part["own"]), dollars(part["socialkit"]-part["own"])))
    pending = "".join('<tr><td>%s</td><td>%s</td><td>%s</td></tr>' %
                      (esc(LABELS.get(p["platform"], p["platform"])), esc(p["drama"]),
                       esc("剧目归因待核实" if p.get("reason") == "drama_attribution_unverified" else p.get("reason", "待核实"))) for p in report["pending"])
    source_rows = ["SocialKit：" + beijing_time(report["socialkit"].get("source_updated_at"))]
    for platform, sites in report.get("own_source_updated_at", {}).items():
        source_rows.extend("我方 %s / %s：%s" % (LABELS.get(platform, platform), site, beijing_time(value)) for site, value in sorted(sites.items()))
    return '''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>SocialKit 收入差异日报</title><style>
:root{color-scheme:light;--ink:#182b44;--muted:#66768a;--line:#dde5ee;--orange:#be610b}*{box-sizing:border-box}body{margin:0;background:#f3f6fa;color:var(--ink);font:15px/1.65 system-ui,"Microsoft YaHei",sans-serif}main{max-width:1440px;margin:auto;padding:40px 28px}header{border-top:4px solid #e8a04c;padding-top:20px}.eyebrow{font-size:12px;letter-spacing:2px;color:var(--orange);font-weight:700}h1{font-size:32px;line-height:1.3;margin:10px 0}h2{font-size:20px;margin:0 0 14px}.muted{color:var(--muted)}.metrics{display:grid;grid-template-columns:repeat(4,1fr);gap:16px;margin:26px 0}.metric,section{background:white;border:1px solid var(--line);border-radius:14px;padding:22px}.metric strong{display:block;font-size:29px;letter-spacing:-1px;margin:5px 0}.metric span{font-size:13px;color:var(--muted)}section{margin:20px 0}.scroll{overflow:auto}table{width:100%%;border-collapse:collapse;font-size:13px}th{text-align:left;color:var(--muted);background:#f6f8fb;white-space:nowrap}th,td{padding:12px 14px;border-bottom:1px solid var(--line);vertical-align:top}tbody tr:hover{background:#fffaf3}.number{white-space:nowrap;font-variant-numeric:tabular-nums;text-align:right}#details td:nth-child(2){min-width:230px;max-width:350px;overflow-wrap:anywhere}#details td:nth-child(3){font-family:ui-monospace,monospace}#details td:nth-child(4){min-width:160px}.toolbar{display:flex;gap:10px;flex-wrap:wrap;margin-bottom:16px}input,select{border:1px solid var(--line);border-radius:8px;background:white;color:var(--ink);padding:10px;font:inherit}input{flex:1;min-width:220px}.note{border-left:3px solid #e8a04c;padding-left:14px}.small{font-size:13px}details summary{cursor:pointer;font-weight:650}footer{font-size:12px;color:var(--muted);overflow-wrap:anywhere}tr[hidden]{display:none}@media(max-width:750px){main{padding:22px 14px}.metrics{grid-template-columns:repeat(2,1fr);gap:10px}.metric,section{padding:16px}.metric strong{font-size:24px}h1{font-size:26px}}@media print{.toolbar{display:none}main{padding:0}.scroll{overflow:visible}section{break-inside:avoid}#details{font-size:9px}}
</style></head><body><main><header><div class="eyebrow">DRAMAWAVE · DAILY REVIEW</div><h1>SocialKit 收入差异日报</h1><div class="muted">收入区间 %s — %s UTC · 最近 7 个完整日，不含播报当天</div><div class="muted small">播报统计日 %s UTC · 采集时间 %s（北京时间）</div></header>
<div class="metrics"><div class="metric">命中项目<strong>%d</strong><span>平台 × 剧目；涉及 %d 部不同剧目</span></div><div class="metric">历史未查到成功发布<strong>%d</strong><span>该平台该剧全部历史</span></div><div class="metric">收入差距较大<strong>%d</strong><span>与未发布条件可能重叠</span></div><div class="metric">待核实项目<strong>%d</strong><span>单独列出，未视为零收入</span></div></div>
<section><h2>平台汇总</h2><p class="muted small">仅汇总下方命中项目。金额为 USD 原始收入，不扣退款；不代表平台全部收入。</p><div class="scroll"><table><thead><tr><th>平台</th><th>项目数</th><th>SocialKit 收入</th><th>我方收入</th><th>收入差额</th></tr></thead><tbody>%s<tr><td><b>合计</b></td><td>%d</td><td>%s</td><td>%s</td><td>%s</td></tr></tbody></table></div></section>
<section><h2>完整明细</h2><div class="toolbar"><input id="query" aria-label="搜索剧名或 ID" placeholder="搜索剧名或 ID"><select id="platform" aria-label="平台"><option value="">全部平台</option><option value="facebook">Facebook</option><option value="youtube">YouTube</option><option value="x">X</option></select><select id="reason" aria-label="命中原因"><option value="">全部原因</option><option value="no_confirmed_publication">历史未查到成功发布</option><option value="large_revenue_gap">收入差距较大</option></select><select id="sort" aria-label="排序"><option value="gap">按收入差额降序</option><option value="sk">按 SocialKit 收入降序</option></select></div><p id="count" class="muted small" aria-live="polite">共 %d 项</p><div class="scroll"><table id="details"><thead><tr><th>平台</th><th>剧名</th><th>剧目 ID</th><th>命中原因</th><th>SocialKit 收入</th><th>我方收入</th><th>收入差额</th><th>我方占比</th><th>历史成功发布</th><th>最近发布 UTC</th></tr></thead><tbody>%s</tbody></table></div></section>
<section><details><summary>待核实项目（%d）与采集异常</summary><p class="muted small">这些项目未计入命中汇总，需先核实归因或数据完整性。</p><div class="scroll"><table><thead><tr><th>平台</th><th>剧目 ID</th><th>原因</th></tr></thead><tbody>%s</tbody></table></div><p class="small">平台采集异常：%s</p></details></section>
<section><h2>统计口径与数据时间</h2><div class="note">满足任一条件即列入：① SocialKit 区间收入 &gt; $0，且我方该平台该剧全部历史未查到成功发布；② SocialKit 区间收入 ≥ $10，且我方区间收入严格低于其 50%%。</div><p class="small muted">同一平台、同一剧目只计一次；两类原因可以重叠。收入区间与历史发布范围不同。收入差额为 SocialKit − 我方，不表示可直接获得的增量收入。源数据可能回补。此文件为采集时的固定快照，不自动更新。</p><p class="small">源最新更新时间（北京时间）<br>%s</p></section>
<footer>SocialKit 版本：%s · 每天北京时间 11:30 汇总一次 · 完整明细支持离线查看</footer></main><script>
const body=document.querySelector('#details tbody'), rows=Array.from(body.rows);
function update(){const q=document.querySelector('#query').value.trim().toLocaleLowerCase(),p=document.querySelector('#platform').value,r=document.querySelector('#reason').value,s=document.querySelector('#sort').value;rows.sort((a,b)=>Number(b.dataset[s])-Number(a.dataset[s]));let shown=0;for(const row of rows){row.hidden=!!((p&&row.dataset.platform!==p)||(r&&!row.dataset.reasons.split(' ').includes(r))||(q&&!row.textContent.toLocaleLowerCase().includes(q)));if(!row.hidden)shown++;body.appendChild(row)}document.querySelector('#count').textContent=`显示 ${shown} / ${rows.length} 项`}
document.querySelectorAll('input,select').forEach(el=>el.addEventListener('input',update));update();
</script></body></html>''' % (esc(period["start"]), esc(period["end"]), esc(report["date"]), esc(beijing_time(report["collected_at"])),
        sums["count"], sums["dramas"], sums["unpublished"], sums["gap"], len(report["pending"]), "".join(platform_rows),
        sums["count"], dollars(sums["socialkit"]), dollars(sums["own"]), dollars(sums["socialkit"]-sums["own"]),
        sums["count"], "".join(rows), len(report["pending"]), pending or '<tr><td colspan="3">无</td></tr>',
        esc(json.dumps(report["errors"], ensure_ascii=False)) if report["errors"] else "无", "<br>".join(esc(v) for v in source_rows), esc(report["socialkit"]["version"]))


def publish_html(path, slug):
    result = subprocess.run([sys.executable, "/root/codex_test/publish_codex_artifact.py", "html", str(path), "--slug", slug],
                            capture_output=True, text=True, encoding="utf-8", timeout=180)
    if result.returncode:
        raise RuntimeError("html_publish_failed")
    return json.loads(result.stdout)["url"]


def run_digest(*, root=DEFAULT_ROOT, socialkit_root=DEFAULT_SOCIALKIT_ROOT, send=False,
               comparison_file=None, env_files=(), feishu_config=DEFAULT_FEISHU_CONFIG,
               collector=collect, publisher=publish_html, sender=None, clock=None):
    clock = clock or (lambda: datetime.now(timezone.utc))
    now = clock().astimezone(timezone.utc)
    local_day, day = now.astimezone(BEIJING).date().isoformat(), now.date().isoformat()
    root = Path(root).resolve()
    ensure_data_mount(root)
    os.umask(0o077)
    root.mkdir(parents=True, exist_ok=True)
    with run_lock(root):
        store = DeliveryStore(root / "digest-delivery.sqlite3", namespace="socialkit-daily-digest") if send else None
        try:
            prior = store.get(local_day, CHAT_ID) if store else None
            if prior and prior["status"] in ("sent", "sending", "unknown"):
                return dict(status="already_sent" if prior["status"] == "sent" else "held", date=local_day,
                            message_id=prior["message_id"], sent_cards=0)
            if prior and prior["attempts"] >= 3:
                return dict(status="failed", error_code="delivery_retry_limit", date=local_day, sent_cards=0)
            folder = root / "digests" / local_day
            if not send:
                folder = root / "digest-previews" / now.strftime("%Y%m%dT%H%M%SZ")
            folder.mkdir(parents=True, exist_ok=True)
            frozen = folder / "frozen.json"
            if send and frozen.exists():
                package = json.loads(frozen.read_text(encoding="utf-8"))
            else:
                if comparison_file:
                    report = json.loads(Path(comparison_file).read_text(encoding="utf-8"))
                    age = (now - datetime.fromisoformat(report["collected_at"].replace("Z", "+00:00"))).total_seconds()
                    if send and (report["date"] != day or not 0 <= age <= 7200):
                        raise ValueError("comparison_snapshot_stale")
                else:
                    report = collector(settings(env_files), socialkit_root, None, day, now, enforce_fresh=True)
                atomic_json(folder / "comparison.json", report)
                html_path = folder / "report.html"
                html_path.write_text(render_html(report), encoding="utf-8")
                if not send:
                    return dict(status="preview", path=str(html_path), candidate_events=len(report["events"]), pending=len(report["pending"]))
                url = publisher(html_path, "socialkit-gap-" + local_day)
                package = dict(card=summary_card(report, url), html_url=url,
                               candidate_events=len(report["events"]), pending=len(report["pending"]),
                               errors=report["errors"], collected_at=report["collected_at"],
                               period=report.get("period") or comparison_period(report["date"]))
                atomic_json(frozen, package)
            if clock().astimezone(BEIJING).date().isoformat() != local_day or clock().astimezone(timezone.utc).date().isoformat() != day:
                raise ValueError("statistics_day_changed")
            request_uuid = store.claim(local_day, CHAT_ID)
            if not request_uuid:
                return dict(status="already_sent", sent_cards=0, date=local_day)
            try:
                transport = sender or (lambda card, request_id: send_card(feishu_config, CHAT_ID, card, request_id))
                receipt = transport(package["card"], request_uuid)
                if not receipt.get("message_id"):
                    raise RuntimeError("missing_message_id")
            except DefiniteFailure as exc:
                store.finish(local_day, CHAT_ID, "failed", detail=safe_error(exc))
                raise
            except Exception as exc:
                store.finish(local_day, CHAT_ID, "unknown", detail=safe_error(exc))
                raise
            store.finish(local_day, CHAT_ID, "sent", message_id=receipt["message_id"])
            result = dict(status="sent", date=local_day, message_id=receipt["message_id"], request_uuid=request_uuid,
                          sent_cards=1, path=str(folder), **{k: v for k, v in package.items() if k != "card"})
            atomic_json(folder / "receipt.json", result)
            atomic_json(root / "digest-status.json", result)
            return result
        finally:
            if store:
                store.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--send", action="store_true")
    parser.add_argument("--root", default=DEFAULT_ROOT)
    parser.add_argument("--socialkit-root", default=DEFAULT_SOCIALKIT_ROOT)
    parser.add_argument("--comparison-file")
    parser.add_argument("--env-file", action="append", default=[])
    parser.add_argument("--feishu-config", default=DEFAULT_FEISHU_CONFIG)
    args = parser.parse_args(argv)
    try:
        result = run_digest(**vars(args))
    except Exception as exc:
        print(json.dumps(dict(status="failed", error_code=safe_error(exc))))
        return 2
    print(json.dumps(result, ensure_ascii=False))
    return 2 if result["status"] in ("held", "failed") else 0


if __name__ == "__main__":
    raise SystemExit(main())
