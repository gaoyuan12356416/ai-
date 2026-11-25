"""Offline browser coverage for automation rules, preview snapshots, inventory and batch approvals."""

import argparse
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

from playwright.sync_api import expect, sync_playwright

STATIC = Path(__file__).resolve().parents[1] / "static"
BASE = "http://127.0.0.1:18081"
API = "/api/admin/tt-promotion"


class Fixture:
    def __init__(self):
        self.channels = [
            dict(account_id=str(i), nickname=f"频道{i}", eligible=True, enabled=True,
                 revenue_28d=100 if i == 101 else None, drama_language="en" if i == 101 else "fr",
                 audience="女频" if i == 101 else None,
                 legacy_scheduled=False if i == 101 else None, legacy_conflict=False if i == 101 else None)
            for i in (101, 102)
        ]
        self.templates = [
            dict(id=7, name="收入画像", version=3, enabled=True, config=dict(
                account_ids=["101", "102"], ai_copy=True, model="income", caption_template="{desc}",
                schedule=dict(mode="fixed", times=["09:00", "12:00"]),
                drama_rule=dict(roas_min=28), material_rule=dict(sort_by="spend")))
        ]
        self.requests = []
        self.snapshots = {}
        self.inventory = {"880": "available"}
        self.batch = dict(id=52, template_id=7, template_name="收入画像", status="needs_review",
                          trigger_type="manual", created_at="2026-11-25T23:00:00Z")
        self.drafts = {}
        self.stats_error = True
        self.run_error = True
        self.changed_copy = False
        self.fail_sources = {"/channels"}
        self.denied = {}

    @staticmethod
    def candidate(i, account, conflict=False):
        return dict(account_id=account, material_id=None if conflict else str(910+i), content_id="the-show",
                    drama_name="测试剧", scheduled_at_utc="2026-11-26T11:00:00Z",
                    status="skipped" if conflict else "eligible", conflicts=["旧模板 1 冲突"] if conflict else [],
                    reason="preferred_match", drama_metrics=dict(spend=120.5, d0_roas=35.5),
                    material_metrics=dict(spend=99, d0_roas=42), caption="两条不同文案预演", decision={})

    def handle(self, route):
        req = route.request
        url = urlsplit(req.url)
        assert url.hostname == "127.0.0.1", url
        route_path = url.path
        if route_path == "/api/ui/topbar":
            data = dict(authenticated=True, user=dict(user_id="u", tenant_key="t", role="admin", is_admin=True, permissions={}))
        elif route_path == "/navigation.json":
            data = []
        elif route_path.startswith(API):
            ep = route_path[len(API):]
            body = req.post_data_json if req.method == "POST" else {}
            q = dict(parse_qsl(url.query))
            self.requests.append((req.method, ep, q, body))
            status, data = self.respond(ep, req.method, q, body)
            route.fulfill(status=status, json=data)
            return
        else:
            file = STATIC / route_path.lstrip("/")
            if file.is_file() and file.parent == STATIC:
                route.fulfill(path=str(file))
            else:
                route.fulfill(status=404, body="")
            return
        route.fulfill(json=data)

    def respond(self, ep, method, q, body):
        if ep in self.denied:
            return self.denied[ep], dict(message="访问受限")
        if ep in self.fail_sources:
            self.fail_sources.remove(ep)
            return 503, dict(message="单一数据源超时")
        if ep == "/channels":
            return 200, dict(items=self.channels)
        if ep == "/templates":
            if method == "POST":
                self.templates.append(dict(id=8, name=body["name"], version=1, enabled=False, config=body))
            return 200, dict(items=self.templates)
        if ep == "/stats/templates":
            if self.stats_error:
                self.stats_error = False
                return 503, dict(message="统计短暂不可用")
            return 200, dict(items=[dict(template_id=7, next_at="2026-11-28T04:14:00Z", latest=dict(
                id=52, expected=2, failed=1, confirmed=0, skipped=0, review=1, pending=0))])
        if ep == "/history":
            return 200, dict(items=[dict(self.batch, counts=dict(expected=2, failed=1, review=1))],
                             page=int(q["page"]), page_size=25, total=1000)
        if ep == "/runs/52":
            return 200, dict(self.batch, items=[
                dict(id=110, account_id="101", material_id="910", status="published", publish_id="",
                     copy_provenance=dict(source="manual", generated_source="ai", edited=True, draft_id="draft910"),
                     preview_comparison=dict(status="changed", expected_material_id="993", reason="执行时候选变化")),
                dict(id=111, account_id="102", material_id="911", status="failed", error_code="NO_MEDIA",
                     reason="素材复核失败", preview_comparison=dict(status="skipped"))
            ])
        if ep == "/templates/7/inventory":
            if method == "POST":
                original = len(self.inventory)
                self.inventory.update(dict.fromkeys(body["material_ids"], "available"))
                return 200, dict(added=len(self.inventory)-original, total=len(self.inventory))
            return 200, dict(items=[dict(material_id=k, status=v) for k, v in self.inventory.items()],
                             counts=dict(available=len(self.inventory)), total=len(self.inventory), page=1, page_size=25)
        if ep == "/templates/7/preview":
            pid = body.get("preview_id")
            if not pid:
                pid = str(len(self.snapshots)+1)
                ids = body["account_ids"]
                rows = [self.candidate(i, ids[i % len(ids)], conflict=i == 8) for i in range(10 if body.get("plan_date") else len(ids))]
                for row in rows:
                    row["copy_facts_key"] = "facts-" + str(row["material_id"]) + ("-v2" if self.changed_copy and row["account_id"]=="102" else "")
                self.snapshots[pid] = rows
            else:
                rows = self.snapshots[pid]
                if body.get("include_conflict_candidates"):
                    for row in rows:
                        if row["conflicts"]:
                            row["material_id"] = "877"
            total = len(rows)
            offset, limit = body["offset"], body["limit"]
            ready = bool(body.get("preview_id"))
            result = rows if ready else [rows[0]] + [dict(account_id=r["account_id"], status="pending", conflicts=[]) for r in rows[1:]]
            return 200, dict(preview_id=pid, state="ready" if ready else "working",
                             items=result[offset:offset+limit], computed=total if ready else 1, total=total,
                             offset=offset, limit=limit, template_version=3,
                             summary=dict(eligible=sum(r["status"]=="eligible" for r in result),
                                          skipped=sum(r["status"]=="skipped" for r in result),
                                          pending=sum(r["status"]=="pending" for r in result)))
        if ep == "/templates/7/drafts":
            items = []
            for item in body["items"]:
                draft = dict(item, id="draft"+item["material_id"], state="final", source="ai", caption="AI "+item["account_id"])
                self.drafts[draft["id"]] = draft
                items.append(dict(draft, state="generating", caption=""))
            return 200, dict(items=items)
        if ep.startswith("/templates/7/draft/"):
            return 200, self.drafts[ep.rsplit("/", 1)[1]]
        if ep == "/templates/7/run":
            if self.run_error:
                self.run_error = False
                return 503, dict(error="提交连接中断")
            return 200, dict(id=52)
        raise AssertionError((ep, method, body))

    def sent(self, ep, method="POST"):
        return [body for verb, path, _, body in self.requests if verb == method and path == ep]

def run(screens=None):
    fx = Fixture()
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page(viewport=dict(width=1400, height=940), timezone_id="America/Los_Angeles")
        errors = []
        page.on("pageerror", lambda exc: errors.append(str(exc)))
        page.route("**/*", fx.handle)
        page.set_default_timeout(7000)
        page.goto(BASE + "/tt-promotion-auto.html?accounts=101,102,999")
        loc = page.locator
        expect(loc("#channel-source-error")).to_contain_text("频道清单暂未取得")
        expect(loc("#workspace")).to_be_visible()
        expect(loc("#templates")).to_contain_text("收入画像")
        expect(loc("#new-template")).to_be_disabled()
        expect(loc("[data-act=run]")).to_be_disabled()
        loc("[data-view=runs]").click()
        expect(loc("#history-total")).to_contain_text("1000")
        loc("[data-view=templates]").click()
        loc("#channel-source-error button").click()

        def screenshot(name):
            if screens:
                screens.mkdir(parents=True, exist_ok=True)
                page.screenshot(path=str(screens / (name + ".png")))

        def close():
            loc("#dialog .modal-head button").click()
            if loc("#confirm").is_visible():
                loc("#confirm-apply").click()
            expect(loc("#dialog")).not_to_be_visible()

        form = loc("#edit-template")
        expect(form.locator("[data-count]")).to_contain_text("已选 2")
        form.locator("[data-pick=conflict]").select_option("unknown")
        expect(form.locator("[data-accounts] label")).to_have_count(1)
        expect(form.locator("[data-count]")).to_contain_text("已选 2")
        form.locator("[name=name]").fill("双层指标")
        form.locator("[name=drama_spend_min]").fill("4")
        form.locator("[name=drama_spend_max]").fill("1")
        form.locator("[name=material_sort_by]").select_option("spend")
        form.locator("[name=material_sort_direction]").select_option("asc")
        form.locator("[name=material_roas_min]").fill("22.55")
        form.locator("[name=schedule_mode]").select_option("random")
        form.locator("[name=count]").fill("3")
        expect(loc("#edit-summary")).to_contain_text("6 条计划")
        loc("#dialog .modal-head button").click()
        expect(loc("#confirm-title")).to_contain_text("舍弃")
        loc("#confirm-cancel").click()
        form.locator("[type=submit]").click()
        expect(loc("#modal-error")).to_contain_text("下限不能超过上限")
        assert not fx.sent("/templates")
        form.locator("[name=drama_spend_max]").fill("22")
        screenshot("automation-rules")
        form.locator("[type=submit]").click()
        expect(loc("#dialog")).not_to_be_visible()
        loc("#refreshPage").click()
        expect(loc("#new-template")).to_be_enabled()
        expect(loc("#dialog")).not_to_be_visible()
        draft = fx.sent("/templates")[0]
        assert draft["account_ids"] == ["101", "102"]
        assert draft["drama_rule"]["spend_min"] == 4 and draft["drama_rule"]["spend_max"] == 22
        assert draft["material_rule"]["roas_min"] == 22.55 and draft["material_rule"]["sort_direction"] == "asc"

        loc("#template-state").select_option("anomaly")
        loc("[data-to-history=review]").click()
        expect(loc("[name=status]")).to_have_value("review")
        expect(loc("#history-total")).to_contain_text("1000")
        loc("#history-channel").select_option("101")
        loc("[name=start]").fill("2026-11-22")
        loc("[name=end]").fill("2026-11-25")
        loc("[name=trigger_type]").select_option("manual")
        loc("#refresh-runs").click()
        loc("[data-history-page='1']").click()
        expect(loc("#history-pages")).to_contain_text("2 / 40")
        q = [q for _, ep, q, _ in fx.requests if ep == "/history"][-1]
        assert q == dict(template_id="7", account_id="101", trigger_type="manual", status="review",
                         start="2026-11-22", end="2026-11-25", page="2", page_size="25")
        loc("[data-run]").click()
        expect(loc("#task-rows .branch[open]")).to_contain_text("正式结果有变化")
        expect(loc("#task-rows summary")).to_contain_text("待核对")
        expect(loc("#task-rows summary")).not_to_contain_text("已确认发布")
        expect(loc("#task-rows")).to_contain_text("AI 生成，人工修改")
        close()
        loc("[data-view=templates]").click()

        loc("[data-act=inventory]").click()
        expect(loc("#inventory-table")).to_contain_text("未占用 / 待预检")
        loc("#inventory-import textarea").fill("880,881,881")
        loc("#inventory-import [type=submit]").click()
        expect(loc("#inventory-note")).to_contain_text("已加入 1")
        loc("#coverage-preview").click()
        expect(loc("#preview-feedback")).to_contain_text("已完成 2")
        expect(loc("#preview-output")).to_contain_text("覆盖 2 个频道")
        assert not fx.sent("/templates/7/run")
        close()

        loc("[data-act=run]").click()
        f = loc("#execution")
        f.locator("[type=submit]").click()
        expect(loc("#preview-feedback")).to_contain_text("正在试算")
        expect(loc("#preview-output")).to_contain_text("测试剧")
        expect(loc("#execute")).to_be_disabled()
        expect(loc("#preview-feedback")).to_contain_text("已完成 10")
        loc("[data-ppage='1']").click()
        expect(loc("#preview-output")).to_contain_text("旧模板 1 冲突")
        loc("[data-conflict-info]").click()
        expect(loc("#preview-feedback")).to_contain_text("已完成 10")
        loc("[data-pfilter=conflict]").click()
        expect(loc("#preview-output tbody tr")).to_have_count(1)
        expect(loc("#preview-output tbody")).to_contain_text("877")
        loc("[data-pfilter=eligible]").click()
        expect(loc("#preview-output tbody tr")).to_have_count(8)
        expect(loc("#execute")).to_be_hidden()
        f.locator("[name=when]").select_option("at")
        f.locator("[name=scheduled_at]").fill("2026-12-09T12:10")
        f.locator("[name=mode]").select_option("drama")
        f.locator("[name=content_ids]").fill("A,B,A")
        f.locator("[name=resource_ids]").fill("1,DW-A01")
        f.locator("[type=submit]").click()
        expect(loc("#preview-feedback")).to_contain_text("已完成 2")
        expect(loc("#preview-output")).to_contain_text("35.5%")
        expect(loc("#preview-output")).to_contain_text("消耗降序")
        loc("[data-draft-all]").click()
        loc("[data-adopt]").first.click()
        loc("[data-adopt]").first.click()
        expect(loc("#execution [data-accounts] :checked")).to_have_count(2)
        loc("[data-copy-text='101/910']").fill("Edited by operator")
        expect(loc("#review-title")).to_contain_text("2 条采用稿")
        f.locator("[type=submit]").click()
        expect(loc("#preview-feedback")).to_contain_text("已沿用 2 条")
        expect(loc("[data-copy-text='101/910']")).to_have_value("Edited by operator")
        # One candidate changes copy facts on the next preview, even though its material ID is unchanged.
        fx.changed_copy = True
        f.locator("[type=submit]").click()
        expect(loc("#preview-feedback")).to_contain_text("1 条前次用稿")
        expect(loc("#review-title")).to_contain_text("1 条采用稿")
        loc("[data-draft-one='102/911']").click()
        loc("[data-adopt]").click()
        screenshot("automation-preview")
        page.set_viewport_size(dict(width=390, height=844))
        expect(loc("#execute")).to_be_enabled()
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        assert loc("#dialog").evaluate("el => el.scrollWidth <= el.clientWidth + 1")
        screenshot("automation-mobile")
        page.set_viewport_size(dict(width=1400, height=940))
        loc("#execute").click()
        expect(loc("#confirm-text")).to_contain_text("2 个频道")
        loc("#confirm-apply").click()
        expect(loc("#modal-error")).to_contain_text("连接中断")
        loc("#execute").click()
        loc("#confirm-apply").click()
        expect(loc("#dialog-title")).to_have_text("批次 #52")
        expect(loc("#run-buckets")).to_contain_text("待核对 1")
        run1, run2 = fx.sent("/templates/7/run")
        assert run1 == run2, "a transport retry must reuse the same operation"
        assert run1["preview_id"] == list(fx.snapshots)[-1] and run1["account_ids"] == ["101", "102"]
        assert run1["scheduled_at"] == "2026-12-09T04:10:00.000Z"
        assert run1["mode"] == "drama" and run1["content_ids"] == ["A", "B"] and run1["resource_ids"] == ["1", "DW-A01"]
        assert len(run1["drafts"]) == 2 and run1["drafts"][0]["edited_caption"] == "Edited by operator"
        assert "edited_caption" not in run1["drafts"][1] and "manual_caption" not in run1
        fx.fail_sources.add("/templates")
        page.goto(BASE+"/tt-promotion-auto.html")
        expect(loc("#workspace")).to_be_visible()
        expect(loc("#template-source-error")).to_contain_text("模板清单暂未取得")
        expect(loc("#templates-meta")).to_have_text("模板清单暂未取得")
        loc("[data-view=runs]").click()
        expect(loc("#history-total")).to_contain_text("1000")
        loc("#template-source-error button").click()
        loc("[data-view=templates]").click()
        expect(loc("[data-act=run]").first).to_be_enabled()
        for ep, code, msg in [("/channels", 403, "没有访问"), ("/templates", 401, "已过期")]:
            fx.denied = {ep: code}
            page.goto(BASE+"/tt-promotion-auto.html")
            expect(loc("#workspace")).not_to_be_visible()
            expect(loc("#notice")).to_contain_text(msg)
        assert not errors, errors
        browser.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--screens", type=Path)
    run(parser.parse_args().screens)
    print("TT automation tools offline browser regression passed.")

