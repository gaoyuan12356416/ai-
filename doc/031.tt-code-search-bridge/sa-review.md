# SA 需求与技术设计评审

## 结论

有条件通过，可进入实现。关键业务歧义已由产品确认并写入 `requirements.md`：新旧页面隔离、四位 code 全容量回收、`c` 尾部 queue ID、正式渠道 `TT`、Search/Featured 最新 published clone、generic fallback，以及 SQLite 为事实源、Redis 仅缓存。

代码和生产实测尚未完成，本评审不构成发布批准。

## 问题与闭环

| 编号 | 严重级别 | 位置 | 问题 | 结论 / 建议 | 状态 |
| --- | --- | --- | --- | --- | --- |
| SA-01 | P0 | code 生命周期 | 删除最早映射会使历史 code 改指向 | 仅在精确确认 `36^4` 全占用时执行；写审计事件，普通碰撞禁止回收 | 已确认 |
| SA-02 | P0 | Redis | 回收后旧缓存可能误路由 | SQLite 为事实源；`DEL`/覆盖失败旋转 namespace 并旁路 Redis，专项陈旧缓存测试 | 已确认 |
| SA-03 | P0 | 发布幂等 | caption 需要 code，但 code 又属于发布记录 | queue freeze 事务先分配/持久化 code，再一次渲染 caption；同 queue 重试复用 | 已确认 |
| SA-04 | P0 | 同剧搜索 | 同一 content ID 可有多条发布映射 | `published_at DESC, queue_id DESC` 选最新；只 clone 后改 channel | 已确认 |
| SA-05 | P0 | 原页面 | 新需求可能覆盖 `/tt` 文件或路由 | 新 `/tt-code` 文件与 exact route；原文件 hash 作为部署门禁 | 已确认 |
| SA-06 | P1 | Featured UX | 横滑可能触发卡片 click | 触摸/鼠标位移阈值抑制 click；按钮和键盘单独测试 | 已确认 |
| SA-07 | P1 | URL 构造 | 中文、`&`、方括号可破坏参数 | 统一标准 encoder，固定 host/path/参数序和 `af_dp` 一致性校验 | 已确认 |
| SA-08 | P1 | Redis 部署 | 生产当前没有独立 code Redis | 新实例仅 `127.0.0.1:6381`，无公网监听，缓存故障不影响查询 | 已确认 |

## 架构决策记录

| ADR | 决策 |
| --- | --- |
| ADR-01 | 新公开入口为 `/tt-code`，原 `/tt` 完全不动 |
| ADR-02 | 新公开接口为 `GET /api/public/tt-code/resolve?query=...&source=Search|Featured` |
| ADR-03 | 数据表名为 `tt_post_code_route`，保存在现有数据盘 TT SQLite |
| ADR-04 | code 存储统一大写，正则 `^[A-Z0-9]{4}$`，主键唯一 |
| ADR-05 | 正式发布 `af_channel=TT`；直接搜索/Featured clone 仅改为 `Search`/`Featured` |
| ADR-06 | 无 published 映射时使用旧 generic `c=TTpost`、`af_c_id=0001` 并增加对应 channel |
| ADR-07 | Redis env 统一使用 `TT_POST_CODE_REDIS_*`，生产监听 `127.0.0.1:6381` |
| ADR-08 | 成功响应 `query_type=code|content_id`，`route_mode=code_exact|published_clone|generic_fallback` |

## 实施门禁

- 表迁移必须在数据库副本演练并通过 `PRAGMA integrity_check`。
- code 分配、queue 创建和最终 caption 冻结必须位于同一 SQLite 写事务。
- Redis 故障和陈旧值专项测试未通过前不得部署。
- 原 `/tt` 文件 hash 或浏览器回归不一致时停止部署。
- 不得通过真实 publish/canary/run-now 验收。

## PM 修订确认

`requirements.md` v1 已吸收全部 P0/P1 结论。代码评审、测试报告和生产证据待实现后补录。
