# 验证报告

2026-10-20本地实现、独立评审与生产部署完成。实现提交2e28d39，准确manifest与回退见deploy.md。

| 检查 | 结果 |
|---|---|
| 新逐素材时间/素材池/并发SQLite/模拟时钟/模拟Graph | 43通过 |
| 身份broker、runtime及私有HTTP边界 | 19通过 |
| 新迁移/停机/功能回退 | 9通过 |
| 原手动core/来源与链接 | 41 / 30通过 |
| 原网关/SQL broker | 9 / 18通过 |
| 原auto service/store/publisher/manual batch | 68通过 |
| 浏览器视频发布 / Page素材池 | 80 / 47通过 |
| JavaScript语法、Python编译、diff空白检查 | 通过 |

Python上述合计237项；浏览器127项。UI在独立localhost API mock验证，桌面1440、平板820、手机390无外层溢出，已人工查看截图。测试没有访问Graph进行发帖，也没有创建生产短链、加工任务或发布批次。

关键验收包括：立即/预约混用、同素材多Page一致、跨北京午夜配额、旧协议/旧草稿保留、创建人不可被更新覆盖、跨页入池与版本游标、CAS保留输入、原operation恢复、永久每日slot、启停/删除重建/重启防重、最新文案、失败下一条、unknown暂停、账号失效、共享池隐私以及新增开关关闭后的历史执行能力。已知评审问题均已修复，详见bugs和independent-integration-review.md。

本地截图及浏览器结果保存在output/fb-page-pool-ui/，不包含生产凭据，不纳入源码发布。生产既有成功任务1条、不可变短链文件1份；部署前短链集合SHA256为7053156df540516e23183e2f59517f9bc0157ba44ef1c0176f5cdb307d5dbf04。部署后需再次确认未变化。

## 生产验收

37个部署目标一致，5个公网资源HTTP200且SHA一致；25旧表/136085行旧列摘要保留，现有成功任务与短链未变化。主API、FB sidecar、发布worker与新scheduler均active，原9个timer状态保持。内部身份实际HTTP200，公网拒绝404。真实登录完成创建人展示、空池默认暂停、英语素材分页与视频两素材独立预约输入检查，测试未保存/入池/启用/提交。线上截图为output/fb-page-pool-ui/live-pool.png与live-material-schedules.png。
