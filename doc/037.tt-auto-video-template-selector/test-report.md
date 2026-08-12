# 测试报告

## 测试结论

离线测试通过；生产 health、无副作用对比和登录态浏览器只读验收待部署后补充。

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

## 遗留风险

- 部署前仍需确认 CPU 18834 与 GPU 8832 无监听、现有 auto 无 in-flight。
- 必须验证 direct-outro 固定片尾 SHA 与 worker health；不能用真实帖子作上线 canary。

## 发布建议

同意按 `deploy.md` 执行备份、精确 commit 部署和只读生产验收；任一步失败即停止并按备份回滚。
