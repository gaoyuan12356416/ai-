# SA 需求评审

## 结论

通过。纯数字 `/s2l/{id}.html` 与 X 历史空间冲突，采用 `/s2l/tt/{queue_id}.html` 是满足短 ID 且不破坏历史链接的最小安全方案。

## 决策记录

| 编号 | 决策 | 结论 |
| --- | --- | --- |
| SA-01 | 品牌披露只改页面默认还是服务端强制 | 服务端冻结与最终发布双重强制 false |
| SA-02 | 新链接用哪个自增 ID | `tt_post_queue.id`，与任务 ID 一致 |
| SA-03 | 是否直接使用 `/s2l/6.html` | 否；生产 X 6.html 已存在 |
| SA-04 | 历史 TT/X 链接 | 原样保留并回归验证 |
| SA-05 | direct-test | 本需求不改变其独立设置和链接 |

## 风险控制

- 并发自增 ID 在数据库写锁内预测和断言。
- 历史 replay 根据存量 short URL 兼容，不强行转换。
- Nginx 只增加精确 `/s2l/tt/数字.html` 路由。
- 生产验证仅 GET/hash/数据库只读，不手动触发 scheduler 或 publish。
