const fs = require("fs");
const http = require("http");
const path = require("path");

function loadPlaywrightTest() {
  try {
    return require("@playwright/test");
  } catch (originalError) {
    const cliFile = require.main && require.main.filename;
    if (!cliFile) throw originalError;
    const resolved = require.resolve("@playwright/test", { paths: [path.dirname(cliFile)] });
    return require(resolved);
  }
}

const { test, expect } = loadPlaywrightTest();

const VIDEO_JOB = "a".repeat(32);
const COVER_JOB = "b".repeat(32);
const XSS_JOB = "c".repeat(32);
let server;
let baseUrl;

test.use({ channel: "chrome" });

test.beforeAll(async () => {
  const staticRoot = path.resolve(process.cwd(), "static");
  server = http.createServer((request, response) => {
    const pathname = new URL(request.url, "http://127.0.0.1").pathname;
    const relative = pathname === "/" ? "drama-synthesis.html" : pathname.replace(/^\//, "");
    const target = path.resolve(staticRoot, relative);
    if (!target.startsWith(staticRoot + path.sep) || !fs.existsSync(target) || !fs.statSync(target).isFile()) {
      response.writeHead(404).end("not found");
      return;
    }
    response.writeHead(200, { "Content-Type": target.endsWith(".js") ? "text/javascript; charset=utf-8" : "text/html; charset=utf-8" });
    fs.createReadStream(target).pipe(response);
  });
  await new Promise(resolve => server.listen(0, "127.0.0.1", resolve));
  baseUrl = `http://127.0.0.1:${server.address().port}`;
});

test.afterAll(async () => {
  await new Promise(resolve => server.close(resolve));
});

async function openFakePage(page) {
  let channelRequests = 0;
  await page.route("**/api/**", async route => {
    const requestUrl = new URL(route.request().url());
    const pathname = requestUrl.pathname;
    if (pathname === `/api/drama-material/jobs/${VIDEO_JOB}`) {
      await route.fulfill({ json: { job_id: VIDEO_JOB, app_id: "1479", status: "done", drama_name: "Video job", output_video_url: "https://media.example.test/video.mp4", output_video_no_bgm_url: "", output_random_template_url: "", cover_16x9_url: "", short_links: [], youtube_publish_tasks: [] } });
      return;
    }
    if (pathname === `/api/drama-material/jobs/${COVER_JOB}`) {
      await route.fulfill({ json: { job_id: COVER_JOB, app_id: "1479", status: "done", drama_name: "Cover job", output_video_url: "", output_video_no_bgm_url: "", output_random_template_url: "", cover_16x9_url: "https://media.example.test/cover.jpg", short_links: [], youtube_publish_tasks: [] } });
      return;
    }
    if (pathname === `/api/drama-material/jobs/${XSS_JOB}`) {
      await route.fulfill({ json: {
        job_id: XSS_JOB, app_id: "1479", status: "done", drama_name: "Audit job",
        output_video_url: "https://media.example.test/video.mp4", output_video_no_bgm_url: "",
        output_random_template_url: "", cover_16x9_url: "", short_links: [], youtube_publish_tasks: [],
        random_template_recipe: {
          version: '\"><img src=x onerror="window.__recipeXss=1">',
          profile: "profile'quoted",
          source: "<script>window.__recipeXss=2</script>",
          asset_set_sha256: "sha&<tag>",
          assets: { border: { name: '\" onmouseover="window.__recipeXss=3' }, corners: { name: "corner<script>bad</script>" } }
        }
      } });
      return;
    }
    if (pathname === "/api/drama-material/youtube/channels") {
      channelRequests += 1;
      expect(requestUrl.searchParams.get("app_id")).toBe("1479");
      await route.fulfill({ json: { items: [{ channel_local_id: "1", channel_id: "UC" + "A".repeat(22), channel_name: "Fake channel", youtube_account_id: "2", comment_eligible: false }] } });
      return;
    }
    if (pathname === "/api/auth/status") {
      await route.fulfill({ json: { enabled: true, authenticated: true, user: { user_id: "qa", name: "QA", is_admin: true, permissions: { drama_synthesis: true } } } });
      return;
    }
    await route.fulfill({ json: { items: [], total: 0, page: 1, page_size: 20 } });
  });
  await page.goto(`${baseUrl}/drama-synthesis.html`, { waitUntil: "domcontentloaded" });
  await page.waitForFunction(() => typeof window.openYoutubePublish === "function" && typeof window.viewJob === "function");
  return () => channelRequests;
}

test("YouTube modal opens after job then app-scoped channel fetch", async ({ page }) => {
  const channelRequests = await openFakePage(page);
  await page.evaluate(jobId => window.openYoutubePublish(jobId), VIDEO_JOB);
  await expect(page.locator("#youtubePublishModal")).toBeVisible();
  await expect(page.locator("#youtubeChannel option")).toHaveCount(1);
  await expect(page.locator("#youtubeSourceKind option")).toHaveCount(1);
  expect(channelRequests()).toBe(1);
});

test("cover-only task disables short and YouTube actions without channel request", async ({ page }) => {
  const channelRequests = await openFakePage(page);
  await page.evaluate(jobId => window.viewJob(jobId), COVER_JOB);
  await expect(page.locator("#jobDetailActions")).toContainText("无可用视频产物");
  await expect(page.locator("#jobDetailActions button", { hasText: "生成短链" })).toBeDisabled();
  await expect(page.locator("#jobDetailActions button", { hasText: "发布到 YouTube" })).toBeDisabled();
  await page.evaluate(jobId => window.openYoutubePublish(jobId), COVER_JOB);
  expect(channelRequests()).toBe(0);
});

test("recipe audit renders hostile values as visible text without DOM injection", async ({ page }) => {
  await page.addInitScript(() => { window.__recipeXss = 0; });
  await openFakePage(page);
  await page.evaluate(jobId => window.viewJob(jobId), XSS_JOB);
  const audit = page.locator('[data-recipe-audit="true"]');
  await expect(audit).toBeVisible();
  await expect(audit).toContainText('<img src=x onerror="window.__recipeXss=1">');
  await expect(audit).toContainText("<script>window.__recipeXss=2</script>");
  await expect(audit).toContainText('\" onmouseover="window.__recipeXss=3');
  await expect(audit.locator("img,script")).toHaveCount(0);
  expect(await page.evaluate(() => window.__recipeXss)).toBe(0);
});
