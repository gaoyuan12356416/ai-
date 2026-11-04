# 实现计划

- 原生发布：guards、claim 顺序、索引/trigger 迁移和失效领取；同素材未决与活跃 Page 写锁分离。
- 统一调度：FB 资源投影、历史锁收敛、attempt 边界、原生 claim fencing 和指定任务只读对账。
- QA 独立编写反例与审查，重点检查不会重复发布或释放后继 generation 的锁。
- 运营恢复限用户指定 Page 的既有任务；15 行 Excel 保持原始跳过事实。
- 部署只覆盖审查文件，从当前 runtime 生成候选，保留已部署 X/YT/TT 差异；GitHub 精确取码后窄重启。

工作树从 c1e8645 建立，先保留线上 coordinator 的 ed2561f overlays（958ca80、dce503c），避免覆盖其他已上线修复。未提交 work/ 仅用于审查。

实际验证命令及结果记录在 test-report.md：Python compile、原生 FB 测试、pipeline FB 隔离测试、跨渠道资源/回执回归、Linux 候选测试和生产只读检查。
