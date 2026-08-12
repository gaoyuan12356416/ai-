# BUG-002 模板与运行页面字段契约不一致

## 发现阶段

2026-08-12 生产 Chrome 空态验收与静态 DTO 对照审查。

## 现象

模板有数据后“下次执行/最近运行”会持续显示空值；运行详情准备时长可能显示破折号秒数，部分任务状态直接显示英文，运行摘要缺失；新建页浏览器标题误写为“编辑”。

## 复现步骤

对照 `features/x_auto_posts` 的真实 DTO 与两个页面读取字段，并用生产 Chrome 打开模板、运行和新建模板页面。

## 期望结果

页面只展示持久化事实，字段名与后端一致，所有真实任务状态使用中文，新建页标题正确。

## 实际结果

前端读取不存在的 next/last、`prepared_duration_sec` 和摘要字段，且状态映射不完整。

## 根因分析

首版 UI 兼容读取覆盖了空态，但列表 DTO 未补最近/下次事实，任务 DTO 的真实时长字段为 `selected_duration_sec`。

## 修复说明

- 模板 DTO 返回真实最近运行；固定计划计算下次时间，随机计划只读取已持久化 plan，GET 不生成计划。
- 冻结模板快照保持纯快照，不混入 live 字段。
- 使用真实时长字段，补齐任务状态中文、计数摘要和错误码中文映射。
- 新建/编辑页动态设置正确标题。

## 影响文件

- `features/x_auto_posts/core.py`
- `features/x_auto_posts/service.py`
- `static/x-auto-publish-runs.js`
- `static/x-auto-publish-template.js`
- `scripts/test_x_auto_post_service.py`
- `scripts/test_x_auto_publish_ui.py`

## 验证命令与结果

- 完整 X auto/admin UI/app-contract 聚焦回归连同部署/静态契约：129 项通过，1 项按 Windows 平台跳过。
- 既有 X bridge/manual/daily/schedule/catchup/account 回归：281/281 通过。
- `py_compile` 与两个 JavaScript `node --check` 通过。

## 回归结论

已部署并完成生产 Chrome 复测：新建页标题正确，运行详情 404 映射为中文，列表空态/筛选正常。无真实发布写入。
