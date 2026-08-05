# 开发计划

## 开发范围

在独立 `/tt-code` 链路增加整页 i18n 和按语言 Featured 缓存，同时保持旧 `/tt` 与现有跳转合同不变。

## 任务拆分

| 任务 | 负责人 | 文件/模块 | 状态 |
| --- | --- | --- | --- |
| 生产语言和值域审计 | Codex | 只读数据库、systemd、数据盘快照 | 已完成 |
| 页面 i18n 与标题调整 | Codex | `static/tt-drama-code-search.*` | 已完成 |
| 分语言排名与原子缓存 | Codex | `features/tt_drama_featured`、refresh script | 已完成 |
| 静态路由与服务配置 | Codex | Nginx、systemd | 已完成 |
| 自动化与浏览器回归 | Codex | tests、scripts、Playwright | 已完成 |
| GitHub-first 部署与线上验收 | Codex | 43.166.187.96 | 待部署 |

## 编译 / 构建命令

```powershell
node scripts/test_tt_drama_code_bridge.js
$env:TT_CODE_PLAYWRIGHT_PACKAGE='D:\codex\.playwright-runtime\node_modules\playwright-core'
$env:TT_CODE_CHROMIUM_EXECUTABLE='C:\Program Files\Google\Chrome\Application\chrome.exe'
node scripts/test_tt_drama_code_browser.js
python -m unittest tests.test_tt_drama_featured_service tests.test_tt_drama_resolver_app_contract
node scripts/test_tt_drama_bridge.js
python -m compileall -q features scripts tests
```

## 风险与依赖

- 依赖已验证只读数据源可用，且昨日每个目标语言至少存在足量正消耗剧。
- 依赖 W2A 资源缓存能解析候选剧的安全封面和标题。
- 部署前必须重新读取生产 current symlink，防止覆盖并行发布。

## 完成记录

- 2026-08-05 完成 22 个生产语言值域审计；榜单语言只取 `ads_custom_source_insight.drama_language`。
- 完成 23 套页面 UI 文案（22 个生产语言加独立简体中文），未知语言回退英文。
- 完成 schema v2 分语言榜单、原子写入、last-known-good 与前后端严格校验。
- 完成 Featured 点击/拖动、搜索和旧 `/tt` 本地回归；生产部署与线上验收待记录。
