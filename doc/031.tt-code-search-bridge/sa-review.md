# SA 需求与技术设计评审

## 结论

需求与技术设计通过，已进入实现完成后的本地验证阶段。生产部署和线上验收尚未执行，本结论不是发布批准。

## 已闭环问题

| 编号 | 级别 | 问题 | 落地结论 | 状态 |
| --- | --- | --- | --- | --- |
| SA-01 | P0 | 满池删除会让历史 code 改指向 | 仅精确确认 `36^4` 全满时按 `created_at,code` 回收；同事务写 recycle audit | 已实现 |
| SA-02 | P0 | code 与 caption 互相依赖 | queue、route、code、最终 caption 同一个 `BEGIN IMMEDIATE` 事务；失败整体回滚 | 已实现 |
| SA-03 | P0 | 同剧多 published 选择不确定 | `published_at DESC, created_at DESC, queue_id DESC` | 已实现 |
| SA-04 | P0 | Redis 可能成为事实源或返回陈旧 route | cache 完整校验、随机 namespace、失效异常旋转、任何缓存异常回退 SQLite | 已实现 |
| SA-05 | P0 | 公共接口绕过既有保护 | 公共 route 经主 app 8787，复用 token bucket/in-flight gate/DramaWave resolver；sidecar 只暴露 bearer 内部接口 | 已实现 |
| SA-06 | P0 | 原 `/tt` 可能被覆盖 | 新页面/JS/Nginx snippet 独立；旧文件零 diff，部署前后 hash 门禁 | 已实现，待线上验证 |
| SA-07 | P1 | 横滑误触卡片 | 7px drag threshold、click 抑制、scroll snap、按钮与键盘行为 | 已实现，待最终浏览器回归 |
| SA-08 | P1 | 旧队列/直接测试 channel 漂移 | 新正式队列 TT；历史无 route pending 和直接测试继续 AIpost | 已实现 |
| SA-09 | P1 | `{code}` 用在直接测试没有 durable identity | 只允许正式队列；直接测试稳定拒绝 `tt_post_code_macro_queue_only` | 已实现 |

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

## 尚未关闭的发布门禁

- 最终全量 TT 回归和当前 diff 的独立 P0/P1 复审。
- 服务器 DB online backup 副本迁移与 `integrity_check=ok`。
- Redis unit/config 在目标 systemd/Redis 版本的验证，及 6381 仅 loopback 监听。
- 公网 `/tt-code` 的移动/桌面手势、一次请求、fallback 和 fail-closed 验证。
- 原 `/tt` 部署前后 hash/行为证明。
- GitHub exact commit、不可变 release、备份 manifest 与可执行回滚点。
- queue/publish ledger 基线证明验收没有触发真实 TikTok 发布。
