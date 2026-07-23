# SA 评审意见

## 结论

有条件通过。必须先完成全局素材唯一占用、账号日唯一约束、三素材成组预检、unknown 禁止补发和生产 composite 基线确认，才允许启用 timer。

## 问题清单

| 编号 | 严重级别 | 位置 | 问题 | 建议 | 状态 |
| --- | --- | --- | --- | --- | --- |
| SA-001 | P0 | 现有幂等 | `source_date + account + material` 无法阻止跨账号/跨日期重复 | 增加规范 `material_key` 全局唯一索引并回填旧 canary | 已采纳 |
| SA-002 | P0 | 每日约束 | 重复 timer/并发 runner 可能给同账号同日排多条 | 增加 `account_id + run_date` 唯一索引与 run_date 唯一批次 | 已采纳 |
| SA-003 | P0 | 失败恢复 | unknown 后换素材补发可能产生两条 Post | `post_creating/unknown` 停止剩余批次，不自动重试 | 已采纳 |
| SA-004 | P0 | 候选选择 | 入队后才发现 HEVC/分辨率异常会破坏三账号成组计划 | 先下载+ffprobe 凑齐三条，再用一个事务入队 | 已采纳 |
| SA-005 | P0 | 生产部署 | canary 分支的 `app.py` 可能落后生产 composite | 部署前比对 live blob/hash；无法确认时禁止覆盖主 API | 待部署门禁 |
| SA-006 | P1 | 调度 | 用户未指定时间，Persistent 首次启用可能当天补跑 | 默认 10:00 CST，配置 start_date 为次日 | 已采纳 |
| SA-007 | P1 | 日志安全 | 管理页面可能暴露错误详情中的密钥 | Sidecar 脱敏、DTO 白名单、后台 Cookie 管理员鉴权 | 已采纳 |

## 决策记录

- 全局排重采用“进入正式队列即永久占用”，明确失败也不自动释放。
- 三账号固定 allowlist，不能按当天任意 active 账号动态扩张。
- 使用现有 `x_post_publish_log` 作为发布日志表，新增批次表与后台只读视图/API。
- 三账号顺序发布；不使用并发媒体上传。

## PM 修订确认

上述意见已写入需求、数据结构、失败语义、验收标准和部署门禁。
