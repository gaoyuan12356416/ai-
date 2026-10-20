# 验收用例

| 范围 | 关键断言 | 自动化 |
|---|---|---|
| campaign归因 | 改名不改归属、同名不同ID不合并、跨渠道同ID隔离、大小写、32位别名唯一、冲突在筛选前处理 | test_social_analytics.py |
| 发布事实 | YT确认公开去重、手动短链零发布、X目标Post/Repost去重、未知不计、relay源不重复 | test_social_analytics.py |
| 权限 | Cookie/tenant/user必需、独立模块、普通本人/租户admin、撤销即时、旧权限继承CAS和回滚 | test_social_analytics_auth.py |
| X身份 | 创建人而非运行人、手动提交tenant、248缺关系、查询失败和多映射报错、冻结重用 | test_x_attribution.py / test_x_attribution_publish.py |
| 历史迁移 | 全量4000原HTML、阶段中断恢复、重复执行、完整回滚、HASH和发布事实恒定、新增事实保留 | test_x_attribution.py / rehearse_social_analytics_snapshot.py |
| 日报 | 共用collector、同名跨租户隔离、历史sent不变、sending/unknown阻断、分片固定UUID、部分渠道一致 | test_social_daily_report.py |
| 前端 | 默认UTC完整7日、最多3维、渠道筛选、CSV/英文剧名、失败非假零、旧入口、权限失效清空、390px | test_social_analytics_ui_browser.js |
| 既有X | 20分钟duration policy、队列及Post原流程、模板身份透传 | scripts/test_x_posts.py 等119项 |

所有测试隔离；fake HTTP不得请求真实X、不得发送Feishu消息。真实快照验收只读源2026-10-19，固定金额169079分，另按点击/访问/激活/充值/退款/成功发布逐项比较。
