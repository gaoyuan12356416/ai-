# 三产品日报测试用例

自动化入口：在 `ops/ai-image-daily` 执行 `python -B -m unittest test_report test_three_products -v`。新增 fixture 完全隔离生产，包含 root 1479/2373/3360、三组 W2A、root 1479 下 Minis、其他产品、来源与实际投放不同的正式素材、删除素材和同源迁移 ID。旧 `test_report.py` 保留 schema 2、上传账本与发布恢复回归。

| 编号 | 场景 | 预期结果 | 优先级 | 验证方式 |
| --- | --- | --- | --- | --- |
| QA-01 | root 与 W2A 关系、Minis/其他产品排除 | 精确六应用；排除清单保留 Minis；不按相似名称推断关系 | P1 | ScopeTests |
| QA-02 | root 缺失、同应用名跨产品、同 ID 归属冲突 | 抛出错误，不返回不完整或歧义 scope | P1 | ScopeTests |
| QA-03 | 正式 ID 被两个来源产品认领 | 排除正式汇总，进入待核对 | P1 | SourceOwnershipTests |
| QA-04 | DramaWave 产出投放于 MoboShort | 产出归 DramaWave；消耗归 MoboShort；详情保留来源 | P1 | ProductAnalysisTests |
| QA-05 | 同正式 ID 同日跨产品投放 | 金额分产品之和等于总额；合计 distinct ID 为 1，分产品各 1 | P1 | ProductAnalysisTests |
| QA-06 | 日/周/前一天比例与近七天 | 完整 10/15–10/21；分子分母重算 ROAS/CTR/CPI，按正式 ID 去重 | P1 | ProductAnalysisTests |
| QA-07 | 旧已删除 ID 与新迁移 ID | 两份消耗均保留；迁移当天原图数为 0；周原图数为 1 | P1 | ProductAnalysisTests、旧迁移回归 |
| QA-08 | 缺旧映射、历史补传、真实零投放 | 不推算原图；历史补传归正式入库日；零消耗、比率为 null | P1 | ProductAnalysisTests |
| QA-09 | 同 content_id 跨产品、来源/负责人/语言/尺寸/优化师 | 剧目不错误合并；每分组轴指标守恒 | P1 | ProductAnalysisTests |
| QA-10 | 同日同产品 app/channel 图片对照 | 仅匹配正消耗 cell；排除其他应用、渠道、视频及全部已识别 AI；负余量阻断 | P1 | ProductAnalysisTests |
| QA-11 | 未映射正式 ID、未知投放应用、未知 AI 池 | 未映射 AI/未知 app 阻断；未确认池仅待核对，不计正式 AI | P1 | ProductAnalysisTests |
| QA-12 | 只读查询失败、无市场日、独立池不守恒 | 原错误传播，不生成伪零成功报告 | P1 | DataReadFailureTests、旧 CatalogTests |
| QA-13 | 发布中断、文件变更、旧日期覆盖、重复同日执行 | 同 manifest 恢复；拒绝篡改、覆盖快照或降级最新日期 | P1 | 发布专用测试、旧 PublishTests |
| QA-14 | 桌面/手机产品切换、日期、筛选、分页、CSV | 页面及导出同产品/条件；历史页可到最新；新入口正确 | P1 | 本地/公网浏览器验收 |
| QA-15 | JSON 加载失败、缩略图失败、原图链接 | 明确错误可重试；缩略图占位；不自动加载原图 | P2 | 浏览器故障注入 |
| QA-16 | 首屏体积与真实加载 | HTML/必要数据 <=150 KiB gzip，无自动原图；三次冷启动目标中位数 <=3 秒 | P1 | artifact manifest + 公网浏览器 |
| QA-17 | 三产品实际只读账目与旧 DramaWave 相同范围 | 分产品/日/渠道守恒，范围与归因差异明确列示 | P1 | 首份真实报告/SQL readback |
| QA-18 | GitHub 版本、备份、回滚及唯一调度 | 精确 SHA，09:00/09:10/09:30 单 timer；旧历史保留，可恢复旧首页 | P1 | 部署回执及 systemd/public readback |

自动化用例实现完成后才执行集成回归；人工/公网项必须另留实际记录，不把单元测试通过视为生产验收。
