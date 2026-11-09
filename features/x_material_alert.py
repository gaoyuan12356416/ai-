"""Independent Feishu delivery for frozen X material-language shortages."""
from __future__ import annotations

import hashlib
import json
import re
import subprocess
from datetime import datetime, timezone

from features.post_daily_report.delivery import DeliveryStore, DeliveryUnknown, DefiniteFailure, send_card
from features.x_posts.material_shortage import EVENT

LANGUAGES = {"en": "英语", "ja": "日语", "ko": "韩语", "es": "西班牙语", "de": "德语",
             "pt-br": "葡萄牙语（巴西）", "pt": "葡萄牙语", "pl": "波兰语", "cs": "捷克语",
             "zh-tw": "繁体中文", "zh-cn": "简体中文", "fr": "法语", "it": "意大利语",
             "id": "印尼语", "th": "泰语", "vi": "越南语", "ar": "阿拉伯语",
             "tr": "土耳其语", "ru": "俄语", "hi": "印地语"}


def event_key(event):
    identity = [event["run_date"], event["publish_time"], int(event["config_version"])]
    return "x-material-shortage-v1/" + hashlib.sha256(json.dumps(identity).encode()).hexdigest()[:24]


def valid_event(event):
    try:
        if event.get("event") != EVENT or not event.get("languages"):
            return False
        datetime.strptime(event["run_date"] + " " + event["publish_time"], "%Y-%m-%d %H:%M")
        int(event["config_version"])
        seen = set()
        for row in event["languages"]:
            language = row["language"]
            if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", language) or language in seen:
                return False
            seen.add(language)
            required, matched, missing = (row[k] for k in ("required", "matched", "missing"))
            if not all(isinstance(n, int) and not isinstance(n, bool) for n in (required, matched, missing)):
                return False
            if not 0 <= matched < required <= 200 or missing != required - matched:
                return False
        return True
    except (KeyError, ValueError, TypeError):
        return False


def build_card(event, mention_id):
    if not re.fullmatch(r"ou_[A-Za-z0-9]+", mention_id):
        raise ValueError("invalid_mention_id")
    activation = event.get("event") == "x_material_alert_enabled_v1"
    title = "X 素材池缺素材提醒已启用" if activation else "X 素材池缺素材提醒"
    if activation:
        body = "后续素材池每批排期选材完成后，如有语言的可用素材不足，将在本群播报具体语言、需要数量、已选数量和缺口。\n\n账号锁定不计为缺素材；长视频会员容量不足会单独提示。同一批次只提醒一次。"
    else:
        if not valid_event(event):
            raise ValueError("invalid_shortage_event")
        lines = ["**排期：%s %s（北京时间）**" % (event["run_date"], event["publish_time"]), ""]
        for row in event["languages"]:
            language = row["language"]
            lines.append("**%s（%s）**：需要 %d 条，已选 %d 条，**缺 %d 条**。" % (
                LANGUAGES.get(language, language), language, row["required"], row["matched"], row["missing"]))
            premium = row.get("rejections", {}).get("x_long_video_requires_premium", 0)
            if premium:
                lines.append("该语言本批有 %d 条超过20分钟的候选因剩余同语言会员账号容量不足而跳过，请优先补充20分钟内视频。" % premium)
        lines += ["", "请及时补充上述语言符合发布条件的视频素材。已选素材继续按原流程发布。"]
        if event.get("scan_limit_reached"):
            lines.append("本次选材已达到扫描范围上限，以上缺口仅代表本批选材结果。")
        body = "\n".join(lines)
    body += '\n\n<at id="%s"></at> 请及时关注并补充素材。' % mention_id
    return {"config": {"wide_screen_mode": True},
            "header": {"template": "blue" if activation else "orange", "title": {"tag": "plain_text", "content": title}},
            "elements": [{"tag": "div", "text": {"tag": "lark_md", "content": body}}]}


class AlertStore(DeliveryStore):
    def __init__(self, path):
        super().__init__(path)
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS alert_event (
              event_key TEXT PRIMARY KEY, event_json TEXT NOT NULL, card_json TEXT NOT NULL,
              created_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS alert_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        """)

    def cursor(self):
        row = self.db.execute("SELECT value FROM alert_meta WHERE key='journal_cursor'").fetchone()
        return row[0] if row else None

    def ingest(self, entries, config):
        count, invalid = 0, 0
        since = datetime.fromisoformat(config["start_at"].replace("Z", "+00:00"))
        with self.db:
            for entry in entries:
                try:
                    event = json.loads(entry.get("MESSAGE", ""))
                except (ValueError, TypeError):
                    event = {}
                if isinstance(event, dict) and event.get("event") == EVENT:
                    observed = datetime.fromtimestamp(int(entry["__REALTIME_TIMESTAMP"]) / 1000000, timezone.utc)
                    if observed >= since:
                        if valid_event(event):
                            count += self._enqueue(event_key(event), event, config)
                        else:
                            invalid += 1
                if entry.get("__CURSOR"):
                    self.db.execute("INSERT OR REPLACE INTO alert_meta VALUES('journal_cursor',?)", (entry["__CURSOR"],))
        return {"queued": count, "invalid_events": invalid}

    def _enqueue(self, key, event, config):
        return self.db.execute("INSERT OR IGNORE INTO alert_event VALUES(?,?,?,?)", (
            key, json.dumps(event, ensure_ascii=False), json.dumps(build_card(event, config["mention_id"]), ensure_ascii=False),
            datetime.now(timezone.utc).isoformat())).rowcount

    def announce(self, config):
        with self.db:
            return self._enqueue("x-material-alert-enabled-v1", {"event": "x_material_alert_enabled_v1"}, config)

    def deliver(self, config, sender=send_card):
        results = []
        rows = self.db.execute("""SELECT e.* FROM alert_event e LEFT JOIN delivery d
            ON d.report_date=e.event_key AND d.chat_id=?
            WHERE d.status IS NULL OR (d.status='failed' AND d.attempts<3)
            ORDER BY e.created_at LIMIT 100""", (config["chat_id"],)).fetchall()
        for row in rows:
            key = row["event_key"]
            prior = self.get(key, config["chat_id"])
            if prior and prior["status"] in ("sending", "unknown"):
                results.append({"event_key": key, "status": "needs_reconciliation"})
                continue
            if prior and prior["attempts"] >= 3:
                results.append({"event_key": key, "status": "delivery_failed", "detail": prior["detail"]})
                continue
            request_uuid = self.claim(key, config["chat_id"])
            if request_uuid is None:
                continue
            try:
                receipt = sender(config["feishu_config"], config["chat_id"], json.loads(row["card_json"]), request_uuid)
            except DefiniteFailure as exc:
                self.finish(key, config["chat_id"], "failed", detail=str(exc))
                results.append({"event_key": key, "status": "failed", "detail": str(exc)})
            except Exception as exc:
                self.finish(key, config["chat_id"], "unknown", detail=type(exc).__name__)
                results.append({"event_key": key, "status": "needs_reconciliation"})
            else:
                self.finish(key, config["chat_id"], "sent", receipt["message_id"])
                results.append({"event_key": key, "status": "sent", "message_id": receipt["message_id"]})
        return results


def read_journal(config, cursor=None):
    since = datetime.fromisoformat(config["start_at"].replace("Z", "+00:00")).astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    command = ["journalctl", "-u", "x-post-schedule.service", "--no-pager", "-o", "json", "--since", since]
    if cursor:
        command += ["--after-cursor", cursor]
    result = subprocess.run(command, capture_output=True, text=True, timeout=30)
    if result.returncode and cursor:
        # A rotated cursor can be replayed from activation; the ledger deduplicates.
        return read_journal(config)
    if result.returncode:
        raise RuntimeError("shortage_journal_unavailable")
    return [json.loads(line) for line in result.stdout.splitlines() if line.strip()]
