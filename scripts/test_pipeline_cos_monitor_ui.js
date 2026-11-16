"use strict";
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { chromium } = require(process.env.PIPELINE_PLAYWRIGHT_PACKAGE || "playwright");
const root = path.resolve(__dirname, "../static");
const origin = "http://fixture.test";
const stage = { stage: "prepare", max_workers: 1, ceiling: 1, scaling_mode: "manual", min_pending: 100,
  active_jobs: 0, eligible_pending: 99, queue_limit: 10, admission_reason: "cos_below_min_pending" };
const cap = { version: 7, hosts: [{host_id: "cos-cloud", ...stage}], global_publish_limit: 8,
  channel_limits: {x: 2, fb: 2, tt: 2, youtube: 2} };
async function main() {
  const options = { headless: true };
  if (process.env.PIPELINE_CHROMIUM_EXECUTABLE) options.executablePath = process.env.PIPELINE_CHROMIUM_EXECUTABLE;
  const browser = await chromium.launch(options);
  try {
    const page = await browser.newPage();
    const writes = [];
    const stamp = new Date().toISOString();
    await page.route("**/*", async route => {
      const url = new URL(route.request().url());
      if (url.origin !== origin) return route.abort();
      const reply = json => route.fulfill({ json });
      if (url.pathname === "/api/ui/topbar") return reply({authenticated:true,user:{is_admin:true,name:"Fixture",role:"admin",permissions:{x_accounts:true}}});
      if (url.pathname.endsWith("/workers")) return reply({scope:"all",generated_at:stamp,processes:[],capacity:cap,
        items:[{host_id:"cos-cloud",label:"COS 云端",last_seen_at:stamp,stages:[stage]}]});
      if (url.pathname.endsWith("/cos-capacity")) {
        assert.equal(route.request().method(), "PUT");
        const data = route.request().postDataJSON();
        writes.push(data);
        Object.assign(stage, {max_workers:data.max_workers, ceiling:data.max_workers, min_pending:data.min_pending,scaling_mode:data.scaling_mode});
        Object.assign(cap.hosts[0],stage);
        cap.version++;
        return reply({ok:true,capacity:cap});
      }
      if (url.pathname.startsWith("/api/")) return reply({scope:"all",totals:{},items:[],channels:[],warnings:[],generated_at:stamp});
      const local=path.resolve(root,"."+url.pathname);
      if (!local.startsWith(root+path.sep) || !fs.existsSync(local)) return route.fulfill({status:404,body:""});
      return route.fulfill({path:local});
    });
    await page.goto(origin+"/pipeline-monitor.html");
    const card=page.locator('[data-worker-host="cos-cloud"]');
    await card.waitFor();
    assert.match(await card.innerText(),/COS 云端/);
    assert.match(await card.innerText(),/99/);
    const form=page.locator('form[data-host="cos-cloud"][data-stage="prepare"]');
    await form.locator('[name="min_pending"]').fill("120");
    await form.locator('[name="scaling_mode"]').selectOption("auto");
    await form.locator('[name="max_workers"]').fill("3");
    await form.getByText("保存",{exact:true}).click();
    await page.waitForFunction(()=>document.querySelector("#pmCapacityVersion").textContent.includes("8"));
    assert.deepEqual(writes[0],{expected_version:7,host_id:"cos-cloud",stage:"prepare",max_workers:3,scaling_mode:"auto",min_pending:120});
    await form.getByText("停止接单",{exact:true}).click();
    await page.waitForFunction(()=>document.querySelector("#pmCapacityVersion").textContent.includes("9"));
    assert.equal(writes[1].max_workers,0);
    console.log("COS monitor form, backlog, manual/auto, version and drain passed");
  } finally {
    await browser.close();
  }
}
main().catch(error=>{console.error(error);process.exitCode=1;});
