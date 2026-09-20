# 实施计划

1. 已只读核对CPU与HK服务/代码/配置/空间，保存脱敏inventory。
2. 在codex/random-subtemplates-80-20260920独立worktree生成42种新设计及追加manifest；艺术素材实现与部署/文档准备并行。
3. 运行素材验证器、CPU目录和冻结配方相关回归；编译新脚本，检查Git diff。
4. 提交GitHub；服务器获取精确提交，使用stage脚本建立新目录及Drama隔离副本，CPU准备相同manifest。
5. 离线建立RGBA v1/v2，真实GPU私有样片覆盖新样式和两个旧目录。
6. 备份配置/触发器，排空，切三个GPU默认池与CPU目录，窄重启和健康验收，恢复触发器。
7. 回填实际SHA、测试、部署和回滚记录，更新维护技能上下文。

编译：python -m py_compile scripts/*random_subtemplates_20260920.py scripts/random_subtemplate_artwork_20260920.py（执行时显式展开文件列表）。
回归：python -m unittest scripts.test_drama_catalog_versions scripts.test_drama_synthesis_cpu_catalog。
