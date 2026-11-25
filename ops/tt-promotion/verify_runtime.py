"""Run as tt-post in a staged runtime. Use only a temporary template database."""
import json
import sys
import tempfile
from pathlib import Path

# Deployment invokes this helper from the candidate runtime, whose native TT
# service, analytics and promotion packages are the ones under test.
sys.path.insert(0,str(Path.cwd()))
from features.tt_promotion import channels
from features.tt_promotion.publishing import Publisher


def main():
    profile=json.loads((Path("/mnt/data-disk/tt-promotion/profiles/current.json")).read_text())
    tenant=next(row["tenant"] for row in profile["facts"] if row.get("tenant"))
    actor={"role":"admin","user_id":"isolated_acceptance","name":"isolated acceptance","tenant_key":tenant}
    catalog=channels.channel_data(actor)
    summaries={c["account_id"]:c for c in catalog["items"]}
    facts=channels.worker_channels(actor)
    eligible=[c for c in facts if c["eligible"] and c["language"]=="en"]
    busy=channels.handoff(actor)
    eligible.sort(key=lambda c:bool(busy[c["account_id"]]["old_templates"] or busy[c["account_id"]]["inflight"]))
    assert eligible
    selected=eligible[:1]
    selected_ids=[r["account_id"] for r in selected]
    richest=max(summaries,key=lambda k:summaries[k].get("revenue_28d") or 0)
    detail=channels.channel_data(actor,detail=richest)
    assert detail["profile"]["windows"]["28d"]["revenue_cents"]==round(100*summaries[richest]["revenue_28d"])
    report={"channels":len(facts),"profile_status":catalog["meta"]["status"],
            "profiles_with_income":sum(bool(x["has_profile"]) for x in summaries.values())}
    with tempfile.TemporaryDirectory() as tmp:
        path=Path(tmp)/"acceptance.sqlite3"
        pub=Publisher(path)
        template=pub.handle("POST","/templates",dict(name="isolated acceptance",account_ids=selected_ids,
            schedule={"mode":"fixed","times":["11:03"]},model="income"),actor,facts)
        assert not template["enabled"]
        data=pub.handle("POST","/templates/%s/preview"%template["id"],{"expected_version":template["version"]},actor,facts)
        rows=data["items"]
        report["preview"]=[{k:r.get(k) for k in ("status","reason","material_id","content_id")} for r in rows]
        assert len(rows)==len(selected)
        assert all(r.get("content_id") and r.get("material_id") for r in rows),report["preview"]
        report["runs_after_preview"]=len(pub.handle("GET","/runs",{},actor,facts)["items"])
        assert report["runs_after_preview"]==0
    print(json.dumps(report,ensure_ascii=False))


if __name__=="__main__":
    main()
