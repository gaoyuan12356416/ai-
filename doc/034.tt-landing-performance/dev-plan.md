# 开发计划

## 开发范围

TT Code Bridge 的静态首屏、Featured 静态产物、封面缓存、Nginx 缓存压缩及对应测试/文档。

## 任务拆分

| 任务 | 负责人 | 文件/模块 | 状态 |
| --- | --- | --- | --- |
| 需求与架构评审 | Codex | `doc/034.tt-landing-performance/` | 已完成 |
| locale HTML/hash JS 构建 | Codex | `static/`、`scripts/build_tt_drama_code_assets.js` | 已完成 |
| 单语言快照与 WebP 缓存 | Codex | `features/tt_drama_featured_assets/`、refresh 脚本 | 已完成 |
| Nginx gzip/cache/locale 路由 | Codex | `deploy/nginx/` | 已完成 |
| 自动化与浏览器回归 | Codex | `tests/`、`scripts/test_tt_drama_code_*` | 进行中 |
| GitHub-first 生产部署 | Codex | 43.166.187.96 | 待执行 |

## 编译 / 构建命令

```bash
python -m py_compile features/tt_drama_featured_assets/*.py scripts/refresh_tt_drama_featured_assets.py
python -m unittest tests.test_tt_drama_featured_assets tests.test_tt_drama_featured_service
node scripts/build_tt_drama_code_assets.js --check
node scripts/test_tt_drama_code_bridge.js
node scripts/test_tt_drama_code_browser.js
git diff --check
```

## 风险与依赖

- 生产 Nginx 1.14.1 的 variable alias、regex alias、gzip 和响应头行为必须先 `nginx -t` 并用临时请求验证。
- Pillow 必须支持 WebP；没有支持时部署保持原图回退，不允许写入虚假缩略图 URL。
- 现有 Featured timer、资源缓存 current symlink 和发布服务不得因本需求被重启或重建。

## 完成记录

- 2026-08-06：建立 `codex/tt-landing-performance-20260806` 分支和需求包。
