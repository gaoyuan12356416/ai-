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
| 聚焦 unittest | 135 | 135 | 0 | 0 |
| 全量 `test_x*.py` | 351 | 351 | 0 | 0 |
| Python 编译 / diff check | 2 | 2 | 0 | 0 |

### 生产验证

| 项目 | 结果 |
| --- | --- |
| GPU | `b6f95f3874a9bb187aa7e8c7faac6254893ba787`，worker/CPU tunnel 均为 v2 health |
| CPU | `7a20f05ecc760a79f3776fded08d47ccfa76d5d8`，sidecar/main API active |
| Linux | b6f95f3 直接相关 185/185、完整 342/342；7a20f05 新增配置/脱敏聚焦测试通过 |
| 池 53 | 源 `182.791667s` -> 输出 `139.0s`，H264/yuv420p、AAC、720x1280，CPU 复检通过 |
| 池 54 | 源 `171.52s` -> 输出 `139.0s`，H264/yuv420p、AAC、720x1280，CPU 复检通过 |
| 恢复 | requested/ready/restored 均为 2；两项原地 `pending`、未绑定、Episode 1、零历史 |
| 历史安全 | 10:06 run 14 保持 `failed_preflight`，queue/log=0；账号 10 粘性绑定未变 |
| 数据库 | integrity `ok`；剧集重复组、`post_creating`、`unknown_outcome` 均为 0 |
| timer | 16:12 CST 恢复 schedule/claim timer；旧 daily timer 保持 masked |

第一次 canary 因 standalone 脚本未读取既有 schedule env 而在下载前返回
`media_host_not_allowed`；未调用 GPU、未恢复池状态。修复提交只增加严格的
schedule 非秘密白名单和异常脱敏，material backfill 合同未改；本地全量由
342 增至 344 项且全部通过。成功报告 SHA-256：
`e908d9d4eb50f1310d9e5189e15b767fcf622f452f6a00892d2cddfdee502471`。

16:20 自然 run 17 的全部媒体预检通过，并新增 pool 2/57/60/131 四份 ready
manifest；pool 131 在自然流程中由 `212.666667s` 裁为 `139.0s`。随后首队列
在任何 X 请求前因磁盘短链域名漂移停止；attempt=0、unknown=0，其余五条没有
publish log。受保护恢复的真实写入强制使用专用 root-owned 审计目录下的新
JSON 报告，拒绝越界、符号链接祖先和覆盖既有文件；最终 queue/log/Post 结果
完成后追加，不新建计划或直接发布。
