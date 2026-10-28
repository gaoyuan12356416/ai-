# 测试用例

数据为临时 SQLite、模拟 Graph 和合成源记录。生产验收仅处理自然命中，不制造测试广告。

| 编号 | 场景与预期 | 覆盖模块 |
| --- | --- | --- |
| C01 | 2 不触发、3 个不同违规 Ad 触发、同 Ad 不重复计数 | store |
| C02 | 首轮存量保存、证据不随窗口过期、新关联可进入 | store、source 集成 |
| C03 | 32 线程入队/抢占唯一、终态不重排 | store |
| C04 | 申诉跨窗口保留、失败扫描未执行行重新归批 | store |
| C05 | 根产品/剧目/语言精确匹配，封面缺失或冲突失败 | source |
| C06 | 文案、链接查询参数、CTA、深链、身份、归因保留 | payload |
| C07 | 动态/轮播/多剧/未知非空字段拒绝 | payload、source |
| C08 | picture 直接传 URL，无下载上传；失败不绑定/不删旧对象 | graph、worker |
| C09 | 同 payload 单次创建，共享旧 Creative 单次 DELETE | store、worker |
| C10 | 写前绑定变化跳过；写后参数漂移暂停 | worker |
| C11 | 单条失败继续，绑定失败不删旧 Creative | worker |
| C12 | 共享/归档/其他引用阻止清理，不扩展修改范围 | graph、worker |
| C13 | 分页不完整暂缓，暂缓不消耗删除次数 | graph、store |
| C14 | 删除失败不重试，不撤销成功替换 | store、worker |
| C15 | 崩溃保留意图与未知，禁止重复 POST/DELETE | store、worker |
| C16 | 共享旧锁、历史回执和未知围栏优先 | store + 既有回归 |
| C17 | Graph 初始/最大 32，单账户 2，24 次/秒统一预算 | graph、throttle |
| C18 | 首批 256 上限保留积压，停止不再写 | worker |
| C19 | 256/257 可通知，晚到回执下一阶段，纯待审转批静默 | store、CLI |
| C20 | 已知新 Creative ID 的未知创建只读核实后允许首次绑定；无 ID 不猜测 | store、worker |
| C21 | 真实 URL 拉取、并发峰值/吞吐/限流、绑定和删除、Feishu 回执 | 生产首批待验证 |
| C22 | 已知封面 Creative 不因源库同步新绑定在下一小时重复转换 | store、worker、source |

用例模块对应 tests/test_cover_*.py。生产 C21 不能由 mock 替代。完整小时扫描、通知和部署回归以最终集成结果为准。
