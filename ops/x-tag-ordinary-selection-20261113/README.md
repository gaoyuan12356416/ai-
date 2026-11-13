# X 画像普通模板选材空值恢复

18:24 手动运行 9000000000023 的普通画像模板未携带 hit_material_publish / hit_drama_publish，ProfileSelector 正常生成 hit=None；AuditStore 在实际预占之前对 None 调用 get，导致23项重试、77项新任务饥饿，没有制作队列或发布日志。

修复将缺失/空 hit 视为空配置，仅执行画像审计和原子预占。显式 hit 仍冻结发布时间。保留在线文件已有的指定剧和指定素材逻辑；生产实际差异仅一行，前置文件 SHA256 b7fa328db6af090edaefb743db260d148d84cb93dc72d9482d28733478c23fa7。

回归：普通模板 hit=None、空配置、明确指定发布时间、底层预占冲突；以及现网组合上的 selector / publisher / service 共63项通过。旧 selector 测试替身增加可选 account_id 参数以适配已上线频道冷却接口，不改生产冷却逻辑。

部署：推送 GitHub 并 fetch 核对 HEAD，停止 x-tag-auto-runner.timer/path 的新领取，等待当前 runner 自然结束且无 claim，再备份自动队列数据库。复制当前组合版本，只覆盖上述 selection.py，原子切换 /opt/x-tag-publisher/current，仅重启 x-tag-auto.service，恢复原 timer/path。不得重启 x-tag-post / 上传或清空队列；未知结果隔离保持原样。无需重置重试时间，原任务会正常恢复领取。

回滚：停止 timer/path，等待当前 selection 结束，指针切回 deployment.json 的 previous，仅重启 x-tag-auto.service，恢复原 timer/path。不得恢复旧数据库、重复创建 run-now 或清理成功记录。
