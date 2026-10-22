# SA 测试用例评审

2026-10-22：新增业务 fixture 覆盖三产品归属、迁移、对照与失败保护；旧 55 项数据及账本测试继续回归，原 4 项发布测试已迁移到新版 20 项发布/缩略图测试。测试设计可用于实现验收，但不替代真实数据和公网浏览器验证。

| 编号 | 覆盖要求 | 处理 |
| --- | --- | --- |
| STR-01 | 防止 fixture 把来源产品与投放产品默认设成一致而漏检 | 显式加入 DramaWave 来源在 MoboShort 花费、同 ID 两产品花费 |
| STR-02 | 原图去重不能意外抹掉旧 ID 花费 | 同来源版本两个正式 ID、旧 ID deleted、新 ID migration 一起断言 |
| STR-03 | 对照的总量正确仍可能分配错误 | 分别断言每产品、全产品、app/channel/resource_type 匹配和负余量失败 |
| STR-04 | 市场查询空/失败不能用零消费通过 | 独立失败 fixture 和零投放 fixture 分开 |
| STR-05 | 视觉、网络、部署项无法由 Python fixture 证明 | QA-14–18 已完成真实公网桌面/390px、三次冷启动、CSV、旧历史保留及唯一timer验收，证据见test-report.md |

修订确认：未引入外部 API 调用、凭证或生产写操作。所有模拟值与真实业务结果隔离。

执行记录：`python -B -m unittest test_three_products -q`，32 项通过。追加验证逐图账本同时包含三产品和 Minis 时精确纳入三产品、SQLite 本体字节不变、成功空查询 sentinel 与缺日错误区分、总额相同但 app/channel 独立池错误仍阻断。整合后独立执行 `python -B -m unittest discover -s ops/ai-image-daily -p '*test*.py' -q`，115 项全部通过；含回滚 health 修复回归。
