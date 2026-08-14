# 开发计划

## 开发范围

TT Code Bridge 搜索区的 code 位置引导、23 语静态构建、同源 WebP 缓存及对应自动化验收。

## 任务拆分

| 任务 | 负责人 | 文件/模块 | 状态 |
| --- | --- | --- | --- |
| 需求和现网基线审计 | Codex | `/tt`、release/Nginx | 已完成 |
| 引导图片优化 | Codex | `static/tt-drama-code-assets/` | 已完成 |
| 响应式引导 UI | Codex | HTML/CSS | 已完成 |
| 23 语 copy 和静态构建 | Codex | JS/build/locales | 已完成 |
| Nginx immutable 路由 | Codex | `deploy/nginx/` | 已完成 |
| 自动化及视觉验收 | Codex | bridge/browser tests | 已完成 |
| GitHub-first 生产发布 | Codex | 43.166.187.96 | 已完成 |

## 编译 / 构建命令

```bash
node --check static/tt-drama-code-search.js
node --check scripts/build_tt_drama_code_assets.js
node scripts/build_tt_drama_code_assets.js --check
node scripts/test_tt_drama_code_bridge.js
$env:TT_CODE_PLAYWRIGHT_PACKAGE='D:\codex\.playwright-runtime\node_modules\playwright-core'
$env:TT_CODE_CHROMIUM_EXECUTABLE='C:\Program Files\Google\Chrome\Application\chrome.exe'
node scripts/test_tt_drama_code_browser.js
git diff --check
```

## 风险与依赖

- 新 release 切换前，`refresh_tt_drama_featured_assets.py` 必须保持现网 SHA-256 `42f49069011206f685a24c4820fc8e1b9f1d51f61fd0af7d9c26268f8d0d759b`。
- 先发布 hashed 图片/JS，再发布 23 份 HTML，避免短暂 404。
- current symlink 与 Nginx docroot 必须成对更新；仅切 current 不会更新 live HTML。
- 发布前新建本次 pre-guide 备份，不复用 8 月 6 日旧备份作为唯一回滚点。

## 完成记录

- 2026-08-14：从干净的 `7c0141fa` worktree 新建 `codex/tt-code-location-guide-20260814`。
- 2026-08-14：WebP 从 467,659 bytes 优化到 46,114 bytes，内容与 3:4 比例不变。
- 2026-08-14：本地静态构建、WebP hash/23语引用门禁、233 项 bridge 断言和 107 项真实 Chrome 检查通过。
- 2026-08-14：提交 `b0775bc5cbaac53d47529ac366b05ed744fe5731` 已推送并部署；生产Chrome、Search、Featured、404、响应头和日志验收通过。
