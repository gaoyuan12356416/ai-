# BUG-001 X 自动发布页面 CSS 未部署

## 发现阶段

2026-08-12 生产 Chrome 实际验收。

## 现象

模板页与运行记录页 HTML/API 正常，但整页无样式；登录提示和无权限提示同时错误显示。

## 复现步骤

1. 使用已登录管理员 Chrome 打开 `/x-auto-publish-templates.html` 或 `/x-auto-publish-runs.html`。
2. 检查 `/x-auto-publish.css` 请求和 `.hidden` 元素计算样式。

## 期望结果

CSS 返回 200 `text/css`，页面正确布局，已登录管理员不显示登录/权限阻断提示。

## 实际结果

CSS 返回 404，样式表规则数为 0，`.hidden` 失效。

## 根因分析

首次静态发布使用 `x-auto-publish-*` glob。该模式不会匹配 `x-auto-publish.css`，导致应用 static 与 Nginx docroot 都缺少 CSS。

## 修复说明

- 从当前不可变 GitHub release 精确补齐两份 CSS，未重启服务。
- 新增 `deploy/x-auto-post-static-files.txt` 作为逐文件部署清单。
- 新增清单完整性、页面引用覆盖与路径安全测试。
- 三个页面为自己的 CSS/JS 使用统一 cache-buster，确保生产静态修复立即替换浏览器旧脚本。
- 三个 HTML shell 通过精确 Nginx location 返回 `Cache-Control: no-store, max-age=0`，并在页面内保留 no-store meta，避免恢复旧 shell 与新资产映射不一致。

## 影响文件

- `deploy/x-auto-post-static-files.txt`
- `scripts/test_x_auto_post_static_deploy.py`
- `doc/039.x-auto-publish-templates/deploy.md`

## 验证命令与结果

- `python -m unittest scripts.test_x_auto_post_static_deploy`：3/3 通过。
- 公开 CSS：200、`text/css`、14750 字节，与 release SHA256 一致。
- Chrome：两个页面样式规则 139 条，登录/权限提示均为 `display:none`。

## 回归结论

已修复。本次 CSS 补正未创建模板或 run、未额外触发 X Post，也未开启 gate。
