"""Exercise the real 13-account operator workload with a temporary promotion ledger."""

import json
import sys
import tempfile
import time
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path.cwd()))
from features.tt_promotion import channels
from features.tt_promotion.pub_store import connection
from features.tt_promotion.publishing import Publisher


def main():
    with connection(channels.AUTO_DB) as old:
        configs = [
            json.loads(row[0])
            for row in old.execute("""SELECT v.config_json FROM tt_auto_template t
          JOIN tt_auto_template_version v ON v.template_id=t.id AND v.version=t.current_version
          WHERE t.enabled=1""")
        ]
    legacy = max(configs, key=lambda config: len(config["account_ids"]))
    ids = list(map(str, legacy["account_ids"]))
    snapshot = json.loads(Path("/mnt/data-disk/tt-promotion/profiles/current.json").read_text())
    actor = dict(
        role="admin",
        tenant_key=next(f["tenant"] for f in snapshot["facts"] if f.get("tenant")),
        user_id="isolated_batch_acceptance",
        name="isolated verification",
    )
    begin = time.monotonic()
    facts = channels.worker_channels(actor)
    visible = [dict(account_id=c["account_id"]) for c in facts]
    assert set(ids) <= {str(c["account_id"]) for c in facts}
    day = (datetime.now(channels.BEIJING).date() + timedelta(days=1)).isoformat()
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "acceptance.sqlite3"
        pub = Publisher(path)
        template = pub.handle(
            "POST",
            "/templates",
            dict(
                name="isolated preview",
                account_ids=ids,
                schedule=legacy["schedule"],
                model="income",
                cooldown_days=7,
            ),
            actor,
            facts,
        )
        api = "/templates/%s/preview" % template["id"]
        params = dict(
            async_preview=True, expected_version=template["version"], plan_date=day, limit=8, offset=0
        )
        before = time.monotonic()
        res = pub.handle("POST", api, params, actor, facts)
        key = res["preview_id"]
        ack = round(time.monotonic() - before, 2)
        first_page_ready = None
        progress = -1
        print("accepted", len(ids), res["total"], "ack_s", ack, flush=True)
        while True:
            if first_page_ready is None and all(x["status"] != "pending" for x in res["items"]):
                first_page_ready = round(time.monotonic() - before, 2)
            if res["computed"] != progress:
                progress = res["computed"]
                print(
                    "checked",
                    progress,
                    "of",
                    res["total"],
                    "seconds",
                    round(time.monotonic() - before, 2),
                    flush=True,
                )
            if res.get("error"):
                raise RuntimeError(res["error"])
            if progress == res["total"] and res["state"] == "ready":
                break
            if time.monotonic() - before > 840:
                raise TimeoutError("isolated 13-channel preview")
            time.sleep(1)
            res = pub.handle(
                "POST",
                api,
                dict(expected_version=template["version"], preview_id=key, offset=0, limit=8),
                actor,
                visible,
            )

        def pages(size):
            out = []
            for offset in range(0, res["total"], size):
                data = pub.handle(
                    "POST",
                    api,
                    dict(expected_version=template["version"], preview_id=key, limit=size, offset=offset),
                    actor,
                    visible,
                )
                out.extend(data["items"])
            return out

        stamp = time.monotonic()
        rows = pages(8)
        assert rows == pages(1) == pages(24)
        page_read = round(time.monotonic() - stamp, 2)
        ok = [r for r in rows if r["status"] == "eligible"]
        assert all(r["status"] in ("eligible", "skipped") for r in rows)
        assert all(r.get("reason") for r in rows if r["status"] == "skipped")
        assert len({r["material_id"] for r in ok}) == len(ok)
        assert len({(r["account_id"], r["content_id"]) for r in ok}) == len(ok)
        with connection(path) as db:
            counts = {
                t: db.execute("SELECT count(*) FROM " + t).fetchone()[0]
                for t in ("ttp_job", "ttp_slot", "tt_auto_task", "ttp_copy")
            }
        assert not any(counts.values())
        print(
            json.dumps(
                dict(
                    accounts=len(ids),
                    slots=len(rows),
                    summary=res["summary"],
                    skipped_reasons=Counter(r["reason"] for r in rows if r["status"] == "skipped"),
                    first_ack_s=ack,
                    first_page_ready_s=first_page_ready,
                    all_pages_read_s=page_read,
                    total_elapsed_s=round(time.monotonic() - begin, 2),
                    ledger=counts,
                ),
                ensure_ascii=False,
            ),
            flush=True,
        )


if __name__ == "__main__":
    main()
