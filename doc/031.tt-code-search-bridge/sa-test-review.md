# SA 测试用例评审

## 结论

测试设计按最终实现扩充到 91 条并作为发布验收基线。最终 diff 全量运行、移动/桌面浏览器和生产 H 类只读证据均已完成；生产回滚未实际切换旧代码，以现成旧 release、备份 manifest 和 Redis 停机降级证明可回退。

## 覆盖检查

| 编号 | 领域 | 用例 | 评审结论 |
| --- | --- | --- | --- |
| TR-01 | 新旧页面隔离 | A01-A05、H05/H08 | 覆盖 exact route、旧文件 hash 和回滚 |
| TR-02 | 五条 Featured/多输入设备 | B01-B11 | 覆盖动态/fallback、touch/mouse/pen/button/keyboard 与误触 |
| TR-03 | schema/code allocator | C01-C16 | 覆盖事务迁移、碰撞、位图、高占用、满池回收和 audit |
| TR-04 | `{code}` 与兼容 | D01-D12 | 覆盖所有正式 queue、preview、UTF-16、直接测试拒绝和历史 pending |
| TR-05 | W2A/归因 | E01-E11 | 覆盖新 TT/af_dp-first、字段编码、冻结重放和旧 AIpost |
| TR-06 | 公共组合 API | F01-F16 | 覆盖三种 mode、最新排序、一次元数据组合、限流/gate/错误/target |
| TR-07 | Redis | G01-G11 | 覆盖完整 cache row、负缓存、namespace、陈旧值、停止和慢 I/O 锁边界 |
| TR-08 | 部署/回滚/零发布 | H01-H09 | 覆盖 GitHub exact、数据盘、DB 副本、备份、服务、回滚和 ledger |

## 评审要求

1. 满容量使用可注入小 alphabet/length 的临时 SQLite 模型，不向生产写 1,679,616 行。
2. B03/B05 必须派发真实事件链并断言 resolver 调用为 0，不能只检查 CSS。
3. 页面搜索/Featured 点击只允许一个 `/api/public/tt-code/resolve`；不得再串行调用 drama resolver。
4. F04/F05 的最新规则精确为 `published_at DESC,created_at DESC,queue_id DESC`。
5. F13 固定为 fail closed：code 或 ID 对应剧无法验证时公共接口返回 404、无 CTA。
6. G07 必须植入与 SQLite 不一致的旧 namespace 值，证明回收/失效后不会复活。
7. G11 必须分别暂停 Redis GET 和 DELETE，并在暂停期间成功获取共享 queue write lock。
8. D11 直接测试 `{code}` 必须在任何 publish 调用前拒绝；D12 必须证明历史无 code queue 仍走 AIpost。
9. H07 要用 queue/run/publish ID 和发布调用基线证明零真实发布，不能只凭“未看到帖子”。

## 当前执行判定

- 最终 Python discover 为 395 tests，Node 新旧 bridge 为 84/53 assertions，编译与 diff 检查均通过。
- 390x844 与 1440x900 生产资源浏览器验证通过，无 console/page error。
- H01-H07 已有完整生产证据；H08 的破坏性生产代码切回未执行。H09 只实际验证了停止/恢复 Redis 与 SQLite 降级，未恢复 unit/config 或切旧代码；旧 release、备份 manifest 和回滚步骤已核验。

## 发布门禁

最终全量有失败、P0/P1 未关闭、浏览器误触/双请求、Redis 慢 I/O 锁边界失败、旧 `/tt` 变化、DB 副本迁移失败或缺少零发布 ledger 证据，均判定“不通过”。
