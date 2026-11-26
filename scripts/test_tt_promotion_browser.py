"""Browser regression for the two independent TT promotion pages. All HTTP is intercepted."""

import argparse
from pathlib import Path
from urllib.parse import urlsplit

from playwright.sync_api import expect, sync_playwright

STATIC = Path(__file__).resolve().parents[1] / "static"
BASE = "http://127.0.0.1:18081"
API = "/api/admin/tt-promotion"


def run(screens=None):
    channels = [
        dict(
            account_id="101",
            nickname="剧场甲",
            drama_language="en",
            eligible=True,
            enabled=True,
            today_published=3,
            today_expected=5,
            revenue_28d=125.50,
            revenue_total=525.50,
            followers=100,
            has_profile=True,
            audience="女频",
            themes=["悬疑"],
        ),
        dict(
            account_id="102",
            nickname="剧场乙",
            drama_language="fr",
            eligible=False,
            enabled=True,
            policy_configured=False,
            reason="请在频道详情中设置观看范围",
            today_published=0,
            today_expected=2,
            revenue_28d=None,
            revenue_total=None,
            followers=200,
            has_profile=False,
            themes=[],
        ),
    ]
    drama = dict(
        content_id="d28",
        name="剧集 A",
        language="en",
        iap_cents=12500,
        iaa_cents=50,
        allocated_cents=12550,
        published_count=3,
    )
    theme = dict(
        key="suspense", name="悬疑", share_bp=10000, revenue_cents=12550, published_count=3, dramas=[drama]
    )
    window = dict(
        start="2026-11-01",
        end="2026-11-24",
        status="ready",
        revenue_cents=12550,
        iap_cents=12500,
        iaa_cents=50,
        published_count=3,
        audiences=[
            dict(
                key="female",
                name="女频",
                share_bp=10000,
                revenue_cents=12550,
                published_count=3,
                children=[theme],
            )
        ],
    )
    templates = []
    snapshots = {}
    mutations = []
    jobs = []
    requests = []
    errors = []
    fail_first_run = True
    auth_state = "ok"
    creator_fails = True
    save_fails = True
    channel_directory_unavailable = True
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page(viewport={"width": 1440, "height": 960}, timezone_id="America/Los_Angeles")
        page.on("pageerror", lambda e: errors.append(str(e)))

        def handle(route):
            nonlocal fail_first_run, creator_fails, save_fails, channel_directory_unavailable
            req = route.request
            url = urlsplit(req.url)
            assert url.hostname == "127.0.0.1", url
            path = url.path
            post = req.post_data_json if req.method == "POST" else {}
            if path == "/api/ui/topbar":
                if auth_state == "expired":
                    route.fulfill(status=401, json={"message": "expired"})
                    return
                if auth_state == "anon":
                    route.fulfill(json={"authenticated": False})
                    return
                data = dict(
                    authenticated=True,
                    user=dict(
                        user_id="u1",
                        tenant_key="test",
                        name="测试运营",
                        role="admin",
                        is_admin=True,
                        permissions={},
                    ),
                )
            elif path == "/navigation.json":
                data = [
                    dict(
                        key="tiktok_platform",
                        label="TT 平台推广",
                        items=[
                            dict(
                                key="ttPromotionChannels",
                                kind="page",
                                label="频道管理（新版）",
                                href="/tt-promotion-channels.html",
                            ),
                            dict(
                                key="ttPromotionAuto",
                                kind="page",
                                label="自动发布（新版）",
                                href="/tt-promotion-auto.html",
                            ),
                        ],
                    )
                ]
            elif path.startswith(API):
                endpoint = path[len(API) :]
                requests.append((req.method, endpoint, post))
                if auth_state == "denied":
                    route.fulfill(status=403, json={"message": "没有访问权限"})
                    return
                if endpoint == "/channels":
                    if channel_directory_unavailable:
                        channel_directory_unavailable = False
                        route.fulfill(status=503, json={"message": "频道服务暂时不可用"})
                        return
                    data = dict(items=channels, meta=dict(end="2026-11-24"))
                elif endpoint == "/channels/101":
                    data = dict(
                        channel=channels[0],
                        profile=dict(
                            windows={
                                "all": window,
                                "28d": window,
                                "7d": dict(
                                    window, revenue_cents=1750, iap_cents=1710, iaa_cents=40, audiences=[]
                                ),
                            }
                        ),
                        meta=dict(stale=True),
                        handoff=dict(
                            old_templates=[dict(id=4, name="在用旧模板")], new_templates=[], inflight=[]
                        ),
                    )
                elif endpoint == "/channels/102":
                    window_empty = dict(
                        status="unavailable",
                        start="",
                        end="",
                        revenue_cents=None,
                        iap_cents=None,
                        iaa_cents=None,
                        published_count=None,
                        audiences=[],
                    )
                    data = dict(
                        channel=channels[1],
                        profile=dict(
                            windows={"all": window_empty, "7d": window_empty, "28d": window_empty}
                        ),
                        meta=dict(stale=True),
                        handoff={},
                    )
                elif endpoint == "/channels/102/creator-info":
                    if creator_fails:
                        creator_fails = False
                        route.fulfill(status=503, json={"message": "暂时无法取得创作者权限"})
                        return
                    data = dict(
                        creator_info=dict(
                            creator_nickname="剧场乙",
                            creator_username="drama_b",
                            privacy_level_options=["MUTUAL_FOLLOW_FRIENDS", "SELF_ONLY"],
                            comment_disabled=False,
                            duet_disabled=True,
                            stitch_disabled=False,
                        )
                    )
                elif endpoint == "/channels/settings":
                    mutations.append((endpoint, post))
                    if post["settings"].get("publish_policy") and save_fails:
                        save_fails = False
                        route.fulfill(status=409, json={"message": "当前平台权限已变化，请重新检测"})
                        return
                    for item in channels:
                        if item["account_id"] in post["account_ids"]:
                            item.update(post["settings"])
                            if post["settings"].get("publish_policy"):
                                item["policy_configured"] = True
                    data = {"updated": len(post["account_ids"])}
                elif endpoint == "/templates" and req.method == "GET":
                    data = {"items": templates}
                elif endpoint == "/templates" and req.method == "POST":
                    mutations.append((endpoint, post))
                    templates.append(dict(id=7, name=post["name"], version=1, enabled=False, config=post))
                    data = templates[-1]
                elif endpoint == "/templates/7" and req.method == "POST":
                    assert post["expected_version"] == templates[0]["version"]
                    mutations.append((endpoint, post))
                    templates[0].update(name=post["name"], version=2, config=post)
                    data = templates[0]
                elif endpoint == "/templates/7" and req.method == "GET":
                    data = templates[0]
                elif endpoint in ("/templates/7/enable", "/templates/7/disable"):
                    assert post["expected_version"] == 1
                    mutations.append((endpoint, post))
                    templates[0]["enabled"] = endpoint.endswith("/enable")
                    if endpoint.endswith("/disable"):
                        # Simulate a lost response after the server accepted the new state.
                        route.fulfill(status=502, json={"message": "回执中断"})
                        return
                    data = templates[0]
                elif endpoint == "/stats/templates":
                    data = {"items": []}
                elif endpoint == "/history":
                    data = dict(items=jobs, page=1, page_size=25, total=len(jobs))
                elif endpoint == "/templates/7/preview":
                    if post.get("async_preview"):
                        assert post["expected_version"] == 1
                        key = "snapshot-" + str(len(snapshots))
                        slots = post["account_ids"] * (5 if post.get("plan_date") else 1)
                        snapshots[key] = [
                            dict(
                                account_id=a,
                                status="skipped" if a == "102" else "eligible",
                                reason="暂缺授权" if a == "102" else "题材匹配",
                                conflicts=["旧排期占用"] if a == "102" else [],
                                content_id="d28",
                                material_id=str(51 + i),
                                caption=post.get("manual_caption", "预览文案"),
                            )
                            for i, a in enumerate(slots)
                        ]
                    else:
                        key = post["preview_id"]
                    full = snapshots[key]
                    offset, limit = post["offset"], post["limit"]
                    eligible = sum(r["status"] == "eligible" for r in full)
                    data = dict(
                        preview_id=key,
                        state="ready",
                        total=len(full),
                        computed=len(full),
                        offset=offset,
                        limit=limit,
                        items=full[offset : offset + limit],
                        summary=dict(eligible=eligible, skipped=len(full) - eligible, pending=0),
                    )
                elif endpoint == "/templates/7/drafts":
                    assert post["operation_id"] and post["preview_id"] in snapshots
                    assert post["items"] == [{"account_id": "101", "material_id": "59"}]
                    data = {"items": [dict(id=8, state="waiting", account_id="101", material_id="59")]}
                elif endpoint == "/templates/7/draft/8":
                    data = {
                        "id": 8,
                        "state": "final",
                        "caption": "AI 审阅稿 {code}",
                        "source": "ai",
                        "account_id": "101",
                        "material_id": "59",
                    }
                elif endpoint == "/templates/7/run":
                    mutations.append((endpoint, post))
                    assert post["preview_id"] in snapshots and post["drafts"] == []
                    assert post["account_ids"] == ["101", "102"]
                    if fail_first_run:
                        fail_first_run = False
                        route.fulfill(status=503, json={"message": "上次提交结果待核对"})
                        return
                    jobs.append(
                        dict(
                            id=19,
                            template_id=7,
                            template_name=templates[0]["name"],
                            status="completed",
                            trigger_type="manual",
                            items=[
                                dict(
                                    id="401",
                                    account_id="101",
                                    status="published",
                                    publish_id="pid-7",
                                    content_id="d28",
                                    material_id="51",
                                    caption=post["manual_caption"],
                                    copy_source="manual",
                                    publish_url="",
                                )
                            ],
                        )
                    )
                    data = jobs[-1]
                elif endpoint == "/runs":
                    data = {"items": jobs}
                elif endpoint == "/runs/19":
                    data = jobs[0]
                else:
                    raise AssertionError((req.method, endpoint))
            else:
                file = STATIC / path.lstrip("/")
                if file.is_file() and file.parent == STATIC:
                    route.fulfill(path=str(file))
                    return
                route.fulfill(status=404, body="")
                return
            route.fulfill(json=data)

        page.route("**/*", handle)
        page.goto(BASE + "/tt-promotion-channels.html")
        expect(page.locator("#workspace")).to_be_visible()
        expect(page.locator("#list-meta")).to_contain_text("等待取得频道清单")
        page.locator("#list-identity-error button").click()
        expect(page.locator("#channels tr")).to_have_count(2)
        expect(page.locator("#channels")).to_contain_text("待回传")
        page.locator("#filter-toggle").click()
        page.locator("[name=total_min]").fill("500")
        expect(page.locator("#channels tr")).to_have_count(1)
        expect(page.locator("#channels")).to_contain_text("525.50")
        page.locator("[name=total_min]").fill("")
        page.locator("[name=followers_min]").fill("101")
        expect(page.locator("#channels tbody, #channels")).to_contain_text("剧场乙")
        page.locator("[name=followers_min]").fill("")
        page.locator("[name=sort]").select_option("followers_desc")
        expect(page.locator("#channels tr").first).to_contain_text("剧场乙")
        page.locator("[name=sort]").select_option("total_asc")
        expect(page.locator("#channels tr").first).to_contain_text("剧场甲")
        page.locator("#filter-toggle").click()
        page.get_by_role("button", name="剧场甲").click()
        expect(page.get_by_text("在用旧模板", exact=False)).to_be_visible()
        page.locator("summary").filter(has_text="女频").click()
        page.locator("summary").filter(has_text="悬疑").click()
        expect(page.get_by_text("剧集 A")).to_be_visible()
        if screens:
            page.screenshot(path=str(screens / "channel-detail.png"))
        page.get_by_role("button", name="近 7 天").click()
        expect(page.locator(".kpis")).to_contain_text("17.50")
        page.get_by_role("button", name="关闭", exact=True).click()
        page.get_by_role("button", name="剧场乙").click()
        expect(page.locator(".kpis")).to_contain_text("待回传")
        expect(page.locator(".kpis")).not_to_contain_text("0.00")
        expect(page.locator("#dialog")).to_contain_text("待设置发布策略")
        assert not any("/creator-info" in path for _, path, _ in requests)
        page.get_by_role("button", name="编辑发布策略").click()
        expect(page.locator("#modal-error")).to_contain_text("暂时无法取得")
        page.get_by_role("button", name="重新读取创作者权限").click()
        policy = page.locator("#channel-policy")
        expect(policy.locator("[name=privacy_level]")).to_have_value("")
        expect(policy.locator("option[value=PUBLIC_TO_EVERYONE]")).to_have_count(0)
        expect(policy.locator("[name=allow_duet]")).to_be_disabled()
        save = policy.get_by_role("button", name="保存新版发布策略")
        expect(save).to_be_disabled()
        policy.locator("[name=privacy_level]").select_option("SELF_ONLY")
        policy.locator("[name=commercial]").check()
        expect(save).to_be_disabled()
        policy.locator("[name=brand_content_toggle]").check()
        expect(save).to_be_disabled()
        expect(policy.locator("#policy-state")).to_contain_text("第三方品牌")
        policy.locator("[name=privacy_level]").select_option("MUTUAL_FOLLOW_FRIENDS")
        policy.locator("[name=allow_comment]").check()
        policy.locator("[name=is_aigc]").check()
        expect(save).to_be_enabled()
        save.click()
        expect(page.locator("#modal-error")).to_contain_text("当前平台权限已变化")
        expect(policy.locator("[name=privacy_level]")).to_have_value("MUTUAL_FOLLOW_FRIENDS")
        policy.get_by_role("button", name="重新检测平台能力").click()
        expect(policy.locator("[name=privacy_level]")).to_have_value("MUTUAL_FOLLOW_FRIENDS")
        expect(policy.locator("[name=allow_comment]")).to_be_checked()
        save.click()
        expect(page.locator("#dialog")).to_contain_text("已保存该频道的新版发布策略")
        assert mutations[-1][1] == {
            "account_ids": ["102"],
            "settings": {
                "publish_policy": {
                    "privacy_level": "MUTUAL_FOLLOW_FRIENDS",
                    "allow_comment": True,
                    "allow_duet": False,
                    "allow_stitch": False,
                    "brand_organic_toggle": False,
                    "brand_content_toggle": True,
                    "is_aigc": True,
                }
            },
        }
        page.get_by_role("button", name="关闭", exact=True).click()
        page.locator("[name=language]").select_option("fr")
        expect(page.locator("#channels tr")).to_have_count(1)
        page.get_by_role("checkbox", name="选择当前结果").check()
        page.get_by_role("button", name="重置").click()
        expect(page.locator("#channels tr")).to_have_count(2)
        page.get_by_role("button", name="批量设置（1）").click()
        page.locator("[name=participation]").select_option("no")
        page.get_by_role("button", name="保存频道设置").click()
        expect(page.locator("#notice")).to_contain_text("已保存 1")
        assert mutations[-1][1] == {"account_ids": ["102"], "settings": {"enabled": False}}

        page.goto(BASE + "/tt-promotion-auto.html")
        page.get_by_role("button", name="创建模板").click()
        form = page.locator("#edit-template")
        form.locator("[name=name]").fill("收入选材草稿")
        form.locator("[data-add]").click()
        expect(page).to_have_url(BASE + "/tt-promotion-auto.html?editor=new")
        expect(page.locator("dialog[open]")).to_have_count(0)
        expect(form.locator("#edit-summary")).to_contain_text("2 条计划")
        assert form.locator(".editor-summary").bounding_box()["x"] > form.locator(".editor-main").bounding_box()["x"]
        if screens:
            page.screenshot(path=str(screens / "tt-template-editor.png"))
        form.locator("[data-search]").fill("剧场甲")
        expect(form.locator("[data-accounts] input")).to_have_count(1)
        expect(form.locator("[data-count]")).to_contain_text("已选 2")
        form.locator("[data-search]").fill("")
        form.locator(".resource-options summary").click()
        form.locator('[name=resource_type][value="5"]').check()
        form.locator(".resource-options summary").click()
        form.locator("[name=material_duration_min_seconds]").fill("30")
        form.get_by_role("button", name="添加时间点").click()
        form.locator("[type=time]").last.fill("12:30")
        form.get_by_role("button", name="保存草稿").click()
        expect(page.locator("#notice")).to_contain_text("不能重复")
        assert not templates
        form.locator("[type=time]").last.fill("21:00")
        expect(form.locator("#edit-summary")).to_contain_text("4 条计划")
        form.get_by_role("radio", name="每天随机").check()
        form.locator("[name=count]").fill("2")
        form.locator("[name=ai_copy]").select_option("yes")
        form.get_by_role("button", name="保存草稿").click()
        expect(page.locator("#templates")).to_contain_text("收入选材草稿")
        assert templates[0]["config"]["schedule"] == {"mode": "random", "daily_count": 2}
        assert not templates[0]["enabled"] and templates[0]["config"]["ai_copy"]
        assert templates[0]["config"]["drama_rule"]["resource_type_v2"] == ["5"]
        assert templates[0]["config"]["material_rule"]["duration_min_seconds"] == 30
        switch = page.get_by_role("switch", name="收入选材草稿 自动发布")
        expect(switch).to_have_attribute("aria-checked", "false")
        switch.focus()
        switch.press("Space")
        expect(switch).to_have_attribute("aria-checked", "true")
        expect(page.locator("#template-enabled")).to_have_text("1")
        page.get_by_label("模板状态", exact=True).select_option("disabled")
        expect(switch).to_have_count(0)
        page.locator("#template-filters").get_by_role("button", name="重置").click()
        switch.click()
        expect(switch).to_have_attribute("aria-checked", "false")
        expect(page.locator("#template-disabled")).to_have_text("1")
        expect(page.locator("#notice")).to_contain_text("已核对")
        assert [ep for ep, _ in mutations if ep.endswith(("/enable", "/disable"))] == [
            "/templates/7/enable", "/templates/7/disable"]
        assert not any(ep.endswith("/run") for ep, _ in mutations)
        assert not templates[0]["enabled"]
        page.get_by_role("button", name="预演与执行").click()
        form = page.locator("#execution")
        form.get_by_role("button", name="只读预演").click()
        expect(page.locator("#execute")).not_to_be_visible()
        expect(page.locator("#preview-output")).to_contain_text("10 个频道任务")
        page.get_by_role("button", name="下一页").click()
        expect(page.locator("#preview-output")).to_contain_text("2 / 2 页")
        page.get_by_role("button", name="生成单条 AI 稿").first.click()
        page.get_by_role("button", name="采用此条稿件").click()
        expect(form.locator("[name=mode]")).to_have_value("template")
        expect(form.locator("[name=manual_caption]")).to_have_value("")
        expect(form.locator("[data-accounts] :checked")).to_have_count(2)
        expect(page.locator("#adopted-copy")).to_contain_text("素材 59")
        expect(page.locator("#adopted-copy textarea")).to_have_value("AI 审阅稿 {code}")
        page.get_by_role("button", name="关闭", exact=True).click()
        page.get_by_role("button", name="舍弃更改", exact=True).click()
        page.get_by_role("button", name="预演与执行").click()
        form = page.locator("#execution")
        form.locator("[name=mode]").select_option("serial")
        form.locator("[name=content_id]").fill("d28")
        form.locator("[name=inventory]").fill("51\n52")
        form.locator("[name=when]").select_option("at")
        form.locator("[name=scheduled_at]").fill("2030-01-02T15:30")
        form.locator("[name=manual_caption]").fill("人工定稿")
        form.get_by_role("button", name="只读预演").click()
        expect(page.locator("#preview-output")).to_contain_text("旧排期占用")
        assert requests[-1][1] == "/templates/7/preview" and requests[-1][2]["episodes"] == ["51", "52"]
        assert requests[-1][2]["scheduled_at"] == "2030-01-02T07:30:00.000Z"
        assert all(not p.endswith("/run") for p, _ in mutations)
        if screens:
            page.screenshot(path=str(screens / "template-preview.png"))
        form.locator("[name=manual_caption]").fill("人工定稿修订")
        expect(page.locator("#review-footer")).not_to_be_visible()
        form.get_by_role("button", name="只读预演").click()
        page.get_by_role("button", name="确认预约").click()
        page.get_by_role("button", name="预约", exact=True).click()
        expect(page.locator("#modal-error")).to_have_text("上次提交结果待核对")
        page.get_by_role("button", name="确认预约").click()
        page.get_by_role("button", name="预约", exact=True).click()
        expect(page.locator("#dialog-title")).to_contain_text("批次 #19")
        page.locator("#dialog summary").click()
        expect(page.locator("#dialog")).to_contain_text("公开作品链接待取得")
        expect(page.locator("#dialog")).to_contain_text("人工定稿修订")
        attempts = [m for k, m in mutations if k.endswith("/run")]
        assert len(attempts) == 2 and attempts[0]["operation_id"] == attempts[1]["operation_id"]
        page.get_by_role("button", name="关闭", exact=True).click()
        page.set_viewport_size({"width": 390, "height": 844})
        page.get_by_role("button", name="发布模板", exact=True).click()
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        if screens:
            page.screenshot(path=str(screens / "mobile-templates.png"))
        page.locator("#nav-toggle").click()
        expect(page.locator("#quickNav")).to_be_visible()
        page.locator("#nav-toggle").click()
        expect(page.locator("#quickNav")).not_to_be_visible()
        page.get_by_role("button", name="编辑", exact=True).click()
        expect(page).to_have_url(BASE + "/tt-promotion-auto.html?editor=7")
        form = page.locator("#edit-template")
        expect(form.get_by_role("radio", name="每天随机")).to_be_checked()
        expect(form.locator("[name=count]")).to_have_value("2")
        expect(form.locator("[data-type-count]")).to_have_text("已选 1 类")
        expect(form.locator("[name=material_duration_min_seconds]")).to_have_value("30")
        assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
        if screens:
            page.screenshot(path=str(screens / "tt-editor-mobile.png"))
        form.get_by_role("radio", name="固定时间").check()
        form.locator("[type=time]").fill("09:00")
        form.get_by_role("button", name="添加时间点").click()
        form.locator("[type=time]").last.fill("16:15")
        form.get_by_role("button", name="返回模板列表").click()
        page.locator("#confirm-cancel").click()
        expect(form.locator("#edit-times")).to_contain_text("16:15")
        form.get_by_role("button", name="保存新版本").click()
        expect(page.locator("#templates")).to_contain_text("v2")
        assert templates[0]["config"]["schedule"] == {"mode": "fixed", "times": ["09:00", "16:15"]}
        # Fresh accounts do not require an old-page configuration. Only explicitly
        # chosen and currently allowed audiences are saved, independently per account.
        intake = browser.new_page(viewport={"width": 390, "height": 844})
        intake.on("pageerror", lambda e: errors.append(str(e)))
        fresh = [dict(account_id=str(i), nickname=f"新频道{i}", eligible=False, enabled=True,
                      auth_eligible=True, policy_configured=False, drama_language="en") for i in (201,202,203)]
        writes = []
        def intake_api(route):
            path = urlsplit(route.request.url).path
            if path == API + "/channels":
                return route.fulfill(json={"items": fresh + [channels[0]], "meta": {}})
            if path.startswith(API + "/channels/") and path.endswith("/creator-info"):
                aid = path.split("/")[-2]
                choices = ["SELF_ONLY"] if aid=="203" else ["SELF_ONLY","MUTUAL_FOLLOW_FRIENDS"]
                return route.fulfill(json={"creator_info":{"creator_nickname":f"新频道{aid}","privacy_level_options":choices}})
            if path == API + "/channels/settings":
                body = route.request.post_data_json
                aid = body["account_ids"][0]
                assert body["first_use"] and aid in {"201","202"} and len(body["account_ids"])==1
                assert body["settings"]["publish_policy"]["privacy_level"] == "MUTUAL_FOLLOW_FRIENDS"
                assert body["settings"]["publish_policy"]["allow_comment"] is False
                if aid=="202" and not any(item == "failed" for item in writes):
                    writes.append("failed")
                    return route.fulfill(status=409,json={"message":"平台选项已变化"})
                writes.append(aid)
                next(c for c in fresh if c["account_id"]==aid).update(eligible=True,policy_configured=True)
                return route.fulfill(json={"updated":1})
            return handle(route)

        intake.route("**/*",intake_api)
        intake.goto(BASE + "/tt-promotion-channels.html")
        intake.locator('[data-scope="first"]').click()
        expect(intake.locator("#channels tr")).to_have_count(3)
        assert intake.evaluate("document.documentElement.scrollWidth <= innerWidth")
        intake.locator("#select-mobile").check()
        intake.locator("#first-for-selected").click()
        dialog = intake.get_by_role("dialog",name="首次发布设置")
        expect(dialog.locator("[data-row]")).to_have_count(3)
        expect(dialog.locator("[data-save]")).to_be_disabled()
        assert not writes
        dialog.locator("[data-level]").select_option("MUTUAL_FOLLOW_FRIENDS")
        dialog.locator("[data-apply]").click()
        expect(dialog.locator('[data-row="2"]')).to_have_value("")
        dialog.locator("[data-save]").click()
        expect(dialog).to_contain_text("平台选项已变化")
        expect(dialog).to_contain_text("已保存")
        dialog.get_by_role("button",name="重试").click()
        expect(dialog.locator("[data-save]")).to_be_enabled()
        dialog.locator("[data-save]").click()
        expect(dialog).to_contain_text("已确认 2 / 3")
        dialog.get_by_role("button",name="关闭").click()
        expect(intake.locator("#channels tr")).to_have_count(1)
        assert sorted(writes)==["201","202","failed"]
        intake.goto(BASE + "/tt-promotion-auto.html?accounts=203")
        template = intake.locator("#edit-template")
        template.locator("[name=name]").fill("保留编辑内容")
        template.locator("[data-first-use]").click()
        expect(intake.locator(".first-use-dialog [data-row]")).to_have_count(1)
        expect(intake.locator(".first-use-dialog [data-save]")).to_be_disabled()
        intake.locator(".first-use-dialog [data-close]").click()
        expect(template.locator("[name=name]")).to_have_value("保留编辑内容")
        assert len(writes)==3
        intake.close()
        for state, expected_text in [
            ("anon", "请先登录"),
            ("expired", "登录已过期"),
            ("denied", "没有访问"),
        ]:
            auth_state = state
            page.goto(BASE + "/tt-promotion-auto.html")
            expect(page.locator("#notice")).to_contain_text(expected_text)
            expect(page.locator("#workspace")).not_to_be_visible()
            if state != "denied":
                expect(page.locator("#authButton")).to_have_text("登录")
        assert not errors, errors
        browser.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--screens", type=Path)
    args = parser.parse_args()
    if args.screens:
        args.screens.mkdir(parents=True, exist_ok=True)
    run(args.screens)
    print("TT promotion browser regression passed.")
