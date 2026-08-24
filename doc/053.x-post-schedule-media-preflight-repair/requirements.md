# 053.x-post-schedule-media-preflight-repair 需求与技术设计

## 背景

2026-08-24 素材池排期 run 318 冻结 19 条 deferred 队列后，10 条发布成功，7 条因 `invalid_media_codec`、2 条因 `invalid_media_dimensions` 在 X 上传前失败。九条队列的 repair trigger/job/profile/source hash 均为空，publish attempt 均为 0，证明排期链路没有进入已有 GPU 重制能力。

## 目标

- 素材池自动排期在创建队列前完整下载并探测媒体。
- 对 `invalid_media_codec`、`invalid_media_dimensions` 复用已有 GPU repair，重制后再次下载、探测并核对指纹。
- 只有通过完整预检的原始或重制媒体才能冻结为 queue。
- 单条素材预检/重制失败时继续 FIFO 深扫，不阻断可用候选。

## 范围

### 包含

- `scripts/x_post_schedule_runner.py` 的 material schedule 候选预检。
- H.264/yuv420p/AAC、尺寸/宽高比修复，及 repair 审计字段冻结。
- 跨 hydration page 共享 repair budget 和已接收账号状态。

### 不包含

- 不重放或改写 run 318 及九条历史失败队列。
- 不改变 drama schedule 的 deferred/Relay 行为。
- 不改变手动发布、X Auto、daily、catch-up 或最终 X publish 门禁。
- 不以真实 X Post 作为部署测试。

## 用户故事 / 业务规则

1. 编码或尺寸不合规但可重制的素材，应在绑定队列前完成重制。
2. 重制输出必须通过 CPU 重新下载、ffprobe、SHA256、大小、时长和尺寸核对。
3. queue 必须显式冻结 `media_validation_mode=preflight`、最终 URL/指纹和 repair 台账。
4. 不可修复素材保持未绑定并记录中文可操作错误，继续扫描后续 FIFO 候选。
5. 已存在 frozen queue 永远优先读取，禁止因本变更重新选材、重制或重发。

## 技术设计

### 影响模块

- `scripts/x_post_schedule_runner.py`
- `scripts/test_x_post_schedule_runner.py`
- `doc/053.x-post-schedule-media-preflight-repair/`

### 数据结构

无新增字段。复用 queue 现有 repair 和 preflight 字段。

### API / 接口

无对外 API 变化。内部 schedule-plan payload 从 material 的 deferred 空指纹恢复为完整 preflight 指纹；drama payload 不变。

### 异常与边界

- repair service 未配置、不可达、超时、输出过大、profile/指纹/probe 不一致时失败关闭。
- repair 次数受 `X_POST_SCHEDULE_MAX_REPAIRS_PER_RUN` 限制。
- 长视频仍按冻结目标的 token-scoped Premium/同语言 Relay 规则路由。
- 完整预检会增加到点建队耗时；本次为用户明确选择的成功率优先策略，未来异步预热另立需求。

## 验收标准

1. codec 和 dimensions 两类错误均触发一次 repair，输出通过二次探测后冻结。
2. queue payload 包含 preflight 指纹和完整 repair 审计字段，不再是 deferred 空指纹。
3. 第一页坏素材不阻断下一页正常素材，repair_state 在跨页扫描中保持同一对象。
4. existing frozen plan 查询发生在任何下载/GPU repair 之前。
5. drama schedule 仍为 deferred，历史失败行不自动重试。
6. focused、X 全量回归、py_compile 和 diff check 全部通过。
7. 生产部署使用精确 GitHub commit、不可变 release、在线 SQLite 备份和可执行回滚方案。

## 风险与待确认

- 预检与 repair 可能延迟 queue 创建，但不会产生未知 X 写入。
- 本次不恢复历史九条；如需补发，必须另行做 operator-manual 受控恢复。

## 变更记录

- 2026-08-24：用户确认执行素材池排期预检重制。
