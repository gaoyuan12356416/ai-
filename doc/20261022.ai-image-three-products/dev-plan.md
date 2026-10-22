# 开发计划

基线生产日报8424ded609372ca12709fca60e5b0a1bd88cf096；工作区从包含该模块的081cf5dbf2ceb9ff03983f4cb41ebebcc6c8b5a4建立。分支codex/ai-image-three-products-20261022，仅交付ops/ai-image-daily与本需求文档。

1. scope每日冻结。source SQLite只读；自动SQL走63350 FIFO和只读事务。
2. 按产品复用验证过的来源映射，合并排除冲突；analysis_v3产出/投放分离。保留旧分析用于回归。
3. 按需求流程并行页面、发布恢复、独立QA。页面轻量静态懒加载；发布gzip/缩略图/manifest/readback/rollback。
4. 本地unittest discover、Python3.9 AST编译、node --check、git diff --check；QA确认业务和发布故障恢复。
5. GitHub提交推送后CPU fetch精确SHA；prepare-only真实采集，核对事实/旧日报差异。通过后窄切服务，公开回读、浏览器、timer验收。

不写素材数据库、不补传或发布历史。首份不冒充历史09:00快照。

