# 测试用例

## 模板与 API

| ID | 场景 | 预期 |
| --- | --- | --- |
| TPL-01 | 创建多账号模板 | 创建版本 1，模板停用 |
| TPL-02 | 编辑时 expected_version 过期 | 409，不生成版本 |
| TPL-03 | 复制模板 | 配置相同、ID 不同、停用 |
| TPL-04 | 停用有执行中任务的模板 | 不再排期，原任务继续 |
| TPL-05 | 停用模板手动执行 | 确认后 202，每账号一任务 |
| TPL-06 | 同账号属于多个启用模板 | 两模板均可排期，不互相覆盖 |
| TPL-07 | 账号串行优先级 | inflight > manual > scheduled(due, template_id) |
| TPL-08 | 写 API 非 same-origin / 无权限 | 403 |
| TPL-09 | 手动执行网络未知后用同一幂等键重试 | 返回同一 run，账号/黑名单依赖不重复产生建 run 副作用 |
| TPL-10 | 刚启用模板时宽限窗口内存在更早计划时刻 | 早于 `enabled_at_utc` 的 slot 不补建 run |
| TPL-11 | tick 快照后模板被停用或编辑 | 建 run 事务复核失败，陈旧版本不产生任务 |
| TPL-12 | 同状态重复启用 | `enabled_at_utc` 不移动，调度事实稳定 |
| TPL-13 | 短剧类型不选择或清空 | 保存为空数组，筛选时不限制类型 |
| TPL-14 | 短剧类型选择多个中文枚举 | 保存对应编号数组，编辑时正确回显 |
| TPL-15 | API 传入枚举备注外编号或 `-1` | 400，不保存模板 |
| TPL-16 | 文案不含 `{{content_id}}` / `{{contect_id}}` | 前端允许提交，API 保存成功；空模板和未知宏仍拒绝 |
| TPL-17 | 文案包含 `{code}` | 前端/API 允许保存；正式任务在 GPU prepare 前分配并冻结唯一四位码，重试复用相同码和文案 |
| TPL-18 | 同一自动任务重复冻结四位码 | broker 返回同一码；不同路由事实返回 409；旧 `tt_post_queue` 行数不变 |
| TPL-19 | 四位码 broker 不可用 | 任务进入可重试状态，不调用 GPU prepare/publish，不生成第二个码 |

## 指标与排序

| ID | 场景 | 预期 |
| --- | --- | --- |
| MET-01 | 不填统计窗口 | 使用最近 7 个完整北京时间日 |
| MET-02 | 自定义 N 天 | 精确读取 N 个 READY 日 |
| MET-03 | 缺一天 active generation | 失败关闭，不按 0 计算 |
| MET-04 | READY 空日 | 作为有效 0 行日 |
| MET-05 | 多日 ROAS | `sum(revenue0)/sum(spend)*100`，不平均日 ROAS |
| MET-06 | spend=0 | roas=None，ROAS 条件不通过，双方向均末尾 |
| MET-07 | inclusive 范围 | 等于上下界均通过 |
| MET-08 | 剧/素材独立排序组合 | 各自字段和方向生效，稳定 tie |
| MET-09 | platform 默认 | 仅 0，不包含 9 |
| MET-10 | 刷新失败 | 不切换 active，旧 generation 可继续读 |

## 剧和素材筛选

| ID | 场景 | 预期 |
| --- | --- | --- |
| SEL-01 | app/status/deploy 不合格 | 剧被排除 |
| SEL-02 | 账号语言不一致 | 剧被排除 |
| SEL-03 | 上线窗口为 0 | 不限制历史上线时间 |
| SEL-04 | resource_type_v2 不匹配 | 剧被排除 |
| SEL-05 | 无题材标签 | 标签不参与业务筛选 |
| SEL-06 | 第一名剧无素材 | 继续下一名剧 |
| SEL-07 | 素材时长边界 | 最小、最大值均通过，>3600 拒绝 |
| SEL-08 | 最终安全 resolver 拒绝一个素材 | 继续同剧下一素材 |
| SEL-09 | resolver 返回剧/语言漂移 | 整次选择失败关闭 |
| SEL-10 | resource_type_v2 为空数组 | 不添加类型条件，其他剧筛选规则照常生效 |

## 黑名单、历史和并发

| ID | 场景 | 预期 |
| --- | --- | --- |
| BL-01 | type=0 series_code 命中 | 排除该系列剧 |
| BL-02 | type=1 data_source_id 命中 | 仅排除对应素材，不跨类型误杀剧 |
| BL-03 | is_delete=1 / 空值 | 忽略 |
| BL-04 | 黑名单读取失败/非法活跃行 | 失败关闭 |
| BL-05 | 冻结前新增黑名单 | 最终复核挡住候选 |
| DUP-01 | 旧五表任一命中 | 素材永久排除 |
| DUP-02 | 两线程/两模板抢同素材 | 只有一个成功，另一个继续候选 |
| DUP-03 | 同 run/account 重试 | 返回原冻结记录 |
| DUP-04 | 准备/发布终态失败 | 素材记录不删除 |
| DUP-05 | 模板冷却 | 同模板窗口内 content_id 被排除，其他模板不受剧冷却影响 |

## 发布安全

| ID | 场景 | 预期 |
| --- | --- | --- |
| PUB-01 | 发布门禁关闭 | 可完成筛选/准备，不调用真实 publish |
| PUB-02 | 相同任务临时重试 | 使用相同 material 和 gpu_job_id |
| PUB-03 | 获得 publish_id 后进程中断 | 仅 reconcile，不重新 publish |
| PUB-04 | 网络未知结果 | 状态 unknown/reconciling，不重初始化 |
| PUB-05 | creator-info 不允许发布 | 失败关闭并记录阶段响应 |
| PUB-06 | 浏览器读取运行详情 | 不返回源/准备媒体 URL或黑名单值，仅返回摘要和 `prepared` |
| PUB-07 | 下游返回非 TikTok、带查询或带凭据 publish URL | 浏览器响应中的 `publish_url` 置空 |

## 调度与执行隔离

| ID | 场景 | 预期 |
| --- | --- | --- |
| SCH-01 | worker 正在执行耗时 GPU prepare | 独立 scheduler 仍可在下一分钟完成 tick |
| SCH-02 | tick 本轮失败但已有排队任务 | execute worker 仍认领已有任务 |
| SCH-03 | 四个 worker 竞争同一账号 | 账本账号 fence 只允许一个 inflight |

## 旧系统回归

- `static/tt-post-pool.html`、`static/tt-account-settings.html`、`features/tt_posts` 无 diff。
- 旧池 UI、账号设置和 app contract 测试全部通过。
- 新页面和代码不引用旧池写 API。
