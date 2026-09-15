# 部署与回滚

主机43.166.187.96；/root/drama_material_service保持数据盘符号链接；公共静态/usr/share/nginx/html；仅重启主API。

GitHub精确检出→服务器测试/编译→meta_video_account_delete_probe.py --job-id 当前任务（仅GET）→deploy_meta_video_account_delete.py prepare RELEASE→暂停原active的9个发布timer、等待service自然空闲、确认无资产执行/预览/重核→apply BACKUP→safe_restart_drama_api.sh→恢复原active timer→公网/真实会话GET/hash/SQLite对比。

基线b0d5790；5个Feature及双份HTML/JS/CSS共11目标。在线备份任务SQLite并核对完整性。新增两表仅由Store幂等创建，历史行不迁移、不改写，所有数据/备份位于已验证数据盘。

回滚同样排空，使用本release/scripts/deploy_meta_video_account_delete.py rollback BACKUP，再worker-aware主API重启并恢复timer。保留当前SQLite/新表/pair回执/unknown锁，不恢复备份台账。prepare额外生成并校验rollback_video_graph.py：旧Video执行入口暂停，避免旧版本将账户模式失败对象改用全局节点DELETE重试；Ad/Creative流程保持。需恢复本账户删除版本后才能继续Video操作。原始备份代码仍完整保留，计划中的rollback_saved/rollback_after明确记录该兼容保护。

最终提交、备份、部署与验收记录待回填。

从安全回滚恢复本版本时，对同一备份执行apply（无需重新prepare）；只允许原baseline或本备份记录的rollback_after精确哈希，其他线上变更仍拒绝。先排空、apply、worker-aware重启，再恢复timer。
