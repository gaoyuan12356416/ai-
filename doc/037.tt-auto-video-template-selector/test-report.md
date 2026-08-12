# 测试报告

## 测试结论

离线与生产验收全部通过；未触发真实 TikTok prepare/publish canary。

## 测试范围

- TT auto 模板校验、CRUD/复制、调度、冻结版本执行、双路由和 health。
- TT auto 页面、主应用路由合同、JavaScript 语法。
- TT GPU direct-outro/random-overlay 全量回归。
- 共享 TT posts GPU client 与发布生命周期回归。

## 执行统计

| 类型 | 数量 | 通过 | 失败 | 阻塞 |
| --- | --- | --- | --- | --- |
| TT auto / app / UI | 132 | 132 | 0 | 0 |
| TT GPU worker | 73 | 73 | 0 | 0 |
| TT posts service | 141 | 141 | 0 | 0 |
| 合计 | 346 | 346 | 0 | 0 |

## 缺陷情况

未发现未解决缺陷。开发期发现端口 8831 已被现有媒体 origin 配置保留，设计已改用 8832，
并增加静态部署合同测试。

## 验证证据

- `python -m unittest ...`：132/132 通过。
- `python scripts/test_tt_gpu_worker.py`：73/73 通过。
- `python scripts/test_tt_posts_service.py`：141/141 通过。
- `py_compile`、`node --check`、`git diff --check`：通过。
- CPU 从 GitHub exact commit 构建 release 后，服务切换前核心测试 59/59 通过。
- CPU health 同时返回 random v3/trim 0 与 direct-outro v2/trim 4.333333；响应不含 URL/凭据。
- GPU random PID `1362906` 保持不变；direct PID `1812308`、tunnel PID `1812341` 均 active，
  两个 health 均 `asset_identity_ready=true`、`direct_post_eligible=true`。
- 部署前后数据库事实保持：模板 `1`、版本 `12`、run `25`、task `145`、publish_id `109`、
  active claim/nonterminal `0`、`integrity_check=ok`。
- 15:06、15:07 的自然 scheduler/runner 均成功；direct work root 文件数为 0。
- 登录态浏览器加载模板 1 v12/enabled：两项可见，历史缺字段选中“随机排重”，未保存。

## 遗留风险

- direct-outro 的真实制作只会在操作人保存选择并由自然调度创建任务后发生；上线验收刻意不创建帖子。
- 14:12 指标刷新曾因只读源查询 `OperationalError` 失败，08:00–13:00 均成功；此状态早于本次部署，
  未手动重跑，保留小时定时器自然重试。

## 发布建议

已按 `deploy.md` 完成备份、精确 commit 部署和只读生产验收，可正常使用。

## 2026-08-12 缓存事件回归

- 本地 TT auto 回归：136/136 通过；`py_compile`、`node --check`、`git diff --check` 通过。
- GitHub exact release 上线前聚焦回归：127/127 通过。
- 生产环境全量测试中的 3 个环境失败（runner 测试环境变量、代码 broker 端口 18832 被占用）
  可在部署前 release 精确复现，不属于本次变更回归。
- 生产缺字段负向校验：HTTP 409 / `tt_auto_video_template_required`；事实计数不变。
- 公网版本化 JS 返回 HTTP 200，HTML/JS SHA 与 release 一致；SQLite `quick_check=ok`。
- 未执行 run-now、未创建真实 TikTok 验收帖；已有自然任务未被中断。
