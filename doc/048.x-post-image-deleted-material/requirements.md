# 048.x-post-image-deleted-material 需求与技术设计

## 背景

X 素材池当前只接受 `ads_custom_source.type=2`、`is_delete=0` 且视频时长有效的素材。2026-08-18 15:26 的批量入池中，活动图片和仍有有效 URL 的软删除视频被标记为不可用。

## 目标

- Dramawave 活动图片可进入 X 素材池并以单图 Post 发布。
- Dramawave 视频即使 `is_delete=1`，只要其余校验通过，仍可发布。
- 历史合并错误可由自然预检重新校验。

## 范围

### 包含

- 全局 X 素材池的入池校验、定时发布和 operator manual 发布。
- JPG/JPEG、PNG、WEBP 静态图片；GIF 按 X `tweet_gif` 单媒体路径处理。
- 图片最大 5 MiB，GIF 最大 15 MiB；视频继续保持 512 MiB、140 秒/Premium 路由。
- 图片不走视频时长、GPU 转码和 Premium relay。

### 不包含

- X Auto 模板、短剧剧集池、TT/Meta 发布规则。
- 多图 Post、图片编辑/压缩、真实 X 测试 Post。
- 放宽产品、语言、短剧映射、deploy_time、安全标签、URL 域名、去重或 Token 规则。

## 用户故事 / 业务规则

1. `type=1,is_delete=0` 的 Dramawave 图片通过源数据校验后进入媒体预检。
2. `type=2,is_delete IN (0,1)` 的 Dramawave 视频均可进入视频预检。
3. 已删除图片及未知素材类型仍拒绝；X Auto 仍只接受活动视频。
4. 图片最终下载内容必须与预检 SHA-256/大小一致，MIME 与 ffprobe 解码结果一致。
5. 图片归因沿用 `af_channel=short`，不改变历史 URL 或现有短/长视频边界。
6. 所有 X 写入继续遵守一次尝试、未知结果停批、队列去重和审计规则。

## 交互与流程

管理页面和 API 请求体不变。旧错误记录先保留审计值；自然候选扫描重新校验后清空或替换为精确原因。

## 技术设计

### 影响模块

- `features/x_posts/selector.py`：按调用路径启用图片/软删除视频。
- `features/x_posts/service.py`：图片下载、探测、上传类别和最终发布守卫。
- `scripts/x_post_daily_runner.py`：图片预检分支；schedule/manual/catch-up 复用。

### 数据结构

无数据库迁移。图片以 `preflight_duration=0` 冻结，现有指纹、大小、URL、队列和日志字段继续作为审计边界。

### API / 接口

现有 API 路径和请求体不变。X 媒体上传增加 `tweet_image|tweet_gif` 类别。

### 异常与边界

- 图片/GIF 超限、伪造 MIME、无法解码、尺寸无效：预检失败且零 X 写入。
- 已删除视频 URL 已失效：仍由下载失败拦截。
- 历史合并错误重新校验后必须被清除或替换为新的精确阻断原因。

## 验收标准

- 活动 JPG/PNG/WEBP/GIF 和软删除有效视频通过 selector 测试。
- X Auto 的图片/软删除视频仍拒绝。
- 图片预检不调用视频 probe/GPU repair，冻结时长 0。
- 最终发布路径选择 `tweet_image|tweet_gif`，视频类别保持原样。
- 完整 X 回归通过；生产部署不创建 run-now/canary/manual Post。
- 生产 SQLite `quick_check=ok`、FK=0、unknown=0，服务健康且定时器恢复原状态。

## 风险与待确认

- 恢复自然 schedule 后，未来正常时隙可能选中重新变为可用的图片/软删除视频，这是预期业务效果，不作为部署测试主动触发。

## 变更记录

- 2026-08-18：按用户确认的图片与已删除视频发布规则建立需求。
