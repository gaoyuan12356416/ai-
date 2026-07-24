# 013.x-post-media-repair 测试报告

## 当前结论

离线实现与安全审查通过；生产 GPU/COS、当前九条回填和最终服务状态在部署阶段补录。

## 已完成验证

- GPU worker、HTTP 鉴权、NVENC 命令、COS manifest/HEAD 幂等单测。
- daily repair client、一次修复、CPU 二次下载/指纹/probe、FIFO 补位测试。
- 显式 backfill、同锁、零 plan/publish、九条不受 daily 六条上限影响、报告审计测试。
- queue 审计字段与 legacy SQLite 幂等迁移测试。
- X account、OAuth route、material pool、selector、ledger、daily 全套回归。
- Python 语法检查与 `git diff --check`。

当前合计：183 项 unittest 全部通过，失败 0、阻断 0。

## 生产验收待补录

| 项目 | 预期 |
| --- | --- |
| Worker / tunnel / sidecar / timer | active，timer 下一次仍为次日 10:00 |
| 当前九条 | 新 COS URL、CPU 正式 probe 全通过、pool 错误清空 |
| 发布副作用 | queue/log/Post 数量与 backfill 前一致 |
| 数据库 | integrity `ok`，素材/账号日重复计数为 0 |
| 安全 | Token 不出现在输出/journal；worker 仅回环；错误 Bearer 403 |
| 回滚 | CPU/GPU 备份路径和旧 release 指针可用 |
