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

## 2026-07-29 超长裁尾增量

### 本地结论

- `invalid_media_duration` 已进入 CPU/GPU 修复合同；worker 二次 probe 只对
  `>140s` 固定裁到 139 秒，过短、NaN/Inf、损坏、异常 FPS/scan 继续失败。
- codec/dimensions 首错同时掩盖超长时，同次规范化会裁尾；正常时长修复不带
  `-t` 并继续校验源时长保持。
- profile/job/COS/manifest 已升 v2；v1 manifest 不会复用。
- 短剧成功重验要求精确旧错误/集数、未绑定、无历史；恢复脚本先 dry guard，
  所有项全链成功后一次事务清错，不包含 plan/publish。

验证：

| 类型 | 数量 | 通过 | 失败 | 阻塞 |
| --- | ---: | ---: | ---: | ---: |
| 聚焦 unittest | 129 | 129 | 0 | 0 |
| 全量 `test_x*.py` | 342 | 342 | 0 | 0 |
| Python 编译 / diff check | 2 | 2 | 0 | 0 |

### 生产待回填

GPU/CPU commit、release、备份、池 53/54 真实源/输出时长、恢复报告、timer 和
下一自然发布结果在完成生产验证后追加；未取得这些证据前不把增量标记为生产通过。
