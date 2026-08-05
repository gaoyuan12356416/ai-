# SA 需求与技术设计评审

## 结论

需求与技术设计通过，代码、独立复审、生产部署和只读线上验收均已完成。运行代码为 `b01dabe22d9da1571c68b6fb0775a61bb48e18de`，验收没有触发真实 TikTok 发布。

## 已闭环问题

| 编号 | 级别 | 问题 | 落地结论 | 状态 |
| --- | --- | --- | --- | --- |
| SA-01 | P0 | 满池删除会让历史 code 改指向 | 仅精确确认 `36^4` 全满时按 `created_at,code` 回收；同事务写 recycle audit | 已实现 |
| SA-02 | P0 | code 与 caption 互相依赖 | queue、route、code、最终 caption 同一个 `BEGIN IMMEDIATE` 事务；失败整体回滚 | 已实现 |
| SA-03 | P0 | 同剧多 published 选择不确定 | `published_at DESC, created_at DESC, queue_id DESC` | 已实现 |
| SA-04 | P0 | Redis 可能成为事实源或返回陈旧 route | cache 完整校验、随机 namespace、失效异常旋转、任何缓存异常回退 SQLite | 已实现 |
| SA-05 | P0 | 公共接口绕过既有保护 | 公共 route 经主 app 8787，复用 token bucket/in-flight gate/DramaWave resolver；sidecar 只暴露 bearer 内部接口 | 已实现 |
| SA-06 | P0 | 原 `/tt` 可能被覆盖 | 新页面/JS/Nginx snippet 独立；旧文件零 diff，部署前后 hash 门禁 | 已实现，线上 hash 不变 |
| SA-07 | P1 | 横滑误触卡片 | 7px drag threshold、click 抑制、scroll snap、按钮与键盘行为 | 已实现，浏览器回归通过 |
| SA-08 | P1 | 旧队列/直接测试 channel 漂移 | 新正式队列 TT；历史无 route pending 和直接测试继续 AIpost | 已实现 |
| SA-09 | P1 | `{code}` 用在直接测试没有 durable identity | 只允许正式队列；直接测试稳定拒绝 `tt_post_code_macro_queue_only` | 已实现 |
| SA-10 | P1 | 发布日志无法直接核对任务已冻结的短码 | 管理页新增“短码”列，只读取任务 DTO 的 `code`；历史无值、直接测试和非法格式统一显示“—”，不解析描述 | 已实现，线上资源复验通过 |

## 架构决策

| ADR | 决策 |
| --- | --- |
| ADR-01 | 新入口 `/tt-code`，原 `/tt` 完全不动 |
| ADR-02 | 公共 API 是主 app 的组合接口，不将 sidecar 直接暴露公网 |
| ADR-03 | sidecar 使用 `/internal/tt-posts/code-resolve`、loopback 和现有内部 bearer |
| ADR-04 | SQLite `tt_post_code_route` 是事实源，code PK、queue unique；Redis 仅缓存 |
| ADR-05 | Redis 只允许 loopback，生产目标 6381；24h/30s TTL 当前为代码常量 |
| ADR-06 | 所有新正式队列生成 code；`{code}` 是否出现在 caption 不影响分配 |
| ADR-07 | 新正式 URL `af_dp` 第一、channel TT；历史/直接测试保持 AIpost |
| ADR-08 | 直接 ID/Featured 使用最新 published clone；无历史使用 TTpost fallback |
| ADR-09 | code exact 不按 state 过滤，但公共层仍必须确认对应剧存在 |
| ADR-10 | 发布日志短码为只读展示；不新增 API/表字段，不重新分配、不补写历史任务，也不从 caption 提取 |

## 数据与调用流评审

```text
正式 queue freeze
  -> BEGIN IMMEDIATE
  -> queue insert
  -> code 分配 / 满池回收 + audit
  -> route insert + queue.code + 最终 caption
  -> commit

公共查询
  -> Nginx exact route
  -> 主 app 输入/限流/并发门
  -> bearer sidecar 读取 Redis/SQLite route
  -> 主 app DramaWave 剧目校验 + target 二次校验
  -> 一次组合 JSON
```

该边界确保 Redis 不参与唯一性，sidecar 不直接处理公网流量，前端也不需要自己拼接或信任两次异步请求。

## 发布门禁完成情况

- 最终 395 项 TT 回归、Node 新旧 bridge 84/53 assertions 和独立 P0/P1/P2 复审均通过。
- DB online backup 副本迁移幂等、旧行计数不变，生产 `integrity_check=ok`。
- Redis 5/systemd unit 通过首次启动验证，仅监听 `127.0.0.1:6381`；停机时 SQLite fallback 正常。
- 公网 `/tt-code` 已完成 390x844 与 1440x900 验证；Featured 恰好五条，Search/Featured fallback 与 fail-closed 正常。
- 原 `/tt` 三个受保护文件部署前后 SHA-256 完全一致。
- GitHub exact commit、不可变 release、备份 manifest 和旧 release 回滚点齐全。
- queue/publish ledger 部署前后保持 `7 / max 7 / publish IDs 6`，没有为验收触发发布。
