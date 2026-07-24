# 013.x-post-media-repair 测试报告

## 当前结论

离线实现、安全审查和生产验收均已通过。

## 已完成验证

- GPU worker、HTTP 鉴权、NVENC 命令、COS manifest/HEAD 幂等单测。
- daily repair client、一次修复、CPU 二次下载/指纹/probe、FIFO 补位测试。
- 显式 backfill、同锁、零 plan/publish、九条不受 daily 六条上限影响、报告审计测试。
- queue 审计字段与 legacy SQLite 幂等迁移测试。
- X account、OAuth route、material pool、selector、ledger、daily 全套回归。
- Python 语法检查与 `git diff --check`。

当前合计：183 项 unittest 全部通过，失败 0、阻断 0。

## 生产验收

| 项目 | 结果 |
| --- | --- |
| 精确部署版本 | `1f607dff4e4fde1c11931f32ab1d477adf5b610f` |
| Linux 回归 | 183 项 unittest 全部通过，Python 编译通过 |
| Worker / tunnel / sidecar / timer | 全部 active；daily oneshot 为 inactive；下一次为 2026-07-25 10:00 CST |
| 当前九条 | 9/9 GPU 转码、COS HEAD、CPU 下载/指纹/probe 全通过；9 个 manifest 均为 ready |
| 素材池 | 九条保持 unpublished，校验错误全部清空，派生可用状态均为 available |
| 发布副作用 | 回填前后 queue=10、publish log=10、published=10；2026-07-24 queue=9，零新增 |
| 数据库 | integrity `ok`；素材重复组=0；账号日重复组=0 |
| 安全 | Worker 仅监听 127.0.0.1；错误 Bearer 返回 403；修复 Token 未出现在 worker journal |
| 既有 GPU 服务 | `drama-material-api.service` 保持 active |
| 回填报告 | canary 1/1 成功；remaining 8/8 成功；失败 0 |

生产验收时间：2026-07-24 16:28-16:56 CST。
