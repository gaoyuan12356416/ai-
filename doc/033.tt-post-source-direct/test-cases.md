# 测试用例

## 测试范围

覆盖配置、prepare、manifest、发布前再校验、回切隔离和生产非发布验证。

## 测试数据

- 离线固定字节源文件和 mock ffprobe/COS/TikTok API。
- 生产最近 5 个素材仅做只读 ffprobe，素材 ID 不写入测试文档输出。

## 用例列表

| 编号 | 场景 | 步骤 | 预期结果 | 优先级 | 状态 |
| --- | --- | --- | --- | --- | --- |
| TC-001 | source_direct 配置 | 加载 mode 和 trim=0 | profile 为 `tt-post-source-direct-v1`，可正式发布 | P0 | 通过 |
| TC-002 | 禁止裁尾 | 请求 trim>0 | 下载前返回 `source_direct_trim_forbidden` | P0 | 通过 |
| TC-003 | 不执行制作 | prepare 合法原片 | 仅出现 ffprobe，无 FFmpeg 命令 | P0 | 通过 |
| TC-004 | 原字节一致 | 比较源与输出 SHA/大小 | SHA 和大小完全相同 | P0 | 通过 |
| TC-005 | manifest 身份 | 检查 v6/reuse | mode/profile/source SHA/size/URL hash 冻结，重放复用 | P0 | 通过 |
| TC-006 | 发布链路 | 开门禁并使用 mock TikTok | 仍走 `PULL_FROM_URL`，init 一次 | P0 | 通过 |
| TC-007 | 44.1kHz 原片 | H.264 Main + AAC 44.1kHz | 合同通过 | P1 | 通过 |
| TC-008 | 旧模式回归 | 全量 GPU 测试 | branded/clean/outro/local/COS/ledger 无回归 | P0 | 通过 |
| TC-009 | CPU profile 隔离 | 跑 TT core/service/runner 测试 | 仅当前 profile 可领取，历史状态不改 | P0 | 通过 |
| TC-010 | 生产切换 | 检查 CPU/GPU health 和配置 | 两端 mode/profile 对齐，服务 active | P0 | 通过 |
| TC-011 | 无主动发布 | 比较部署前后队列/init 证据 | 部署验证不产生助手触发的真实发布 | P0 | 通过 |
| TC-012 | 回切 | 恢复旧两项配置 | `direct_outro` v2 健康，旧池可继续使用 | P0 | 文档验证 |

## 回归范围

GPU worker、CPU TT Post core/service、prepare runner、direct config、AI 后台 TT route contract、systemd unit/env contract。
