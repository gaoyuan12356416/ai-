# 独立集成与后端复核

复核范围：`pool.py`、`pool_scheduler.py`、`core.py` 的素材池相关增量、身份解析/路由/runtime 集成，以及 `scripts/deploy_fb_page_material_pool.py`。只读审查其他代理负责的实现；发现项由文件负责人修复。本轮不执行生产发布、生产 SQLite 写入或真实短链/GPU/Graph 请求。

## 已发现并修复

1. **P2：永久文案错误码不一致会反复阻塞队首。** 原调度器列入 `caption_too_long`，真实模板模块抛 `text_too_long`，导致入池后 Page 模板变长时反复跳过时点而不移除永久失败素材。现分类包括实际 `text_too_long / unknown_macro / required_text`。`test_latest_template_actual_overflow_consumes_failed_head` 通过真实 `render_preview` 展开 4,900 字节前缀与 200 字节剧名：当前素材转失败，下一时点能创建后续素材任务。
2. **P2：删除重建 Page 后较晚池任务可越过旧冻结任务。** 独立临时 SQLite 复现旧实例素材 21 尚未加工、新实例素材 22 先 ready，旧逻辑按 `pool_instance_id` 检查导致 claim 22。现按同租户、同真实 Page 和永久 slot 序号检查此前未结束池任务；`test_pool_sequence_survives_deleted_recreated_config_instance` 验证旧任务未结束时无 claim，旧任务失败后才放行素材 22。
3. **P2：功能回退未验证静态备份指纹。** 原回退仅核对目标文件，损坏备份仍可能写上线。现所有静态备份在停止服务/写文件前检查 SHA256，恢复前再次复核；`test_corrupted_rollback_backup_is_rejected_before_any_mutation` 验证零写拒绝。安装文件也在等待排空后、复制前检查源 SHA，避免只在重新开放服务后才发现源码变化。
4. **部署迁移失败边界（主代理自审，独立复核）。** ALTER TABLE 后旧版本位置型 INSERT 不再兼容，因此迁移开始即进入不允许重启旧 backend 的阶段。`test_partial_schema_migration_never_restarts_incompatible_old_backend` 验证迁移中断保留现场和 manifest，不将旧代码接回已扩展 SQLite。

## 已核对的关键不变量

- 身份：启用和每个时点均使用当前有效账号，明确校验同租户同 user ID、模块/导航权限、唯一有效邮箱归因；失败不会换成系统身份。临时身份失败只记录跳过且不消费素材，永久身份/授权失效暂停计划。
- 时点：进程启动 cutoff、计划生效时间、永久 `UNIQUE(tenant_key,page_id,bj_date,slot_time)` 三重约束；事务取得锁后再检查 60 秒截止、配置版本、实例和当前队首。删除重建或重新启用不能重新使用当天已占时点。超时源查询不创建任务。
- 原子性：所有外部身份/来源查询在写事务外；slot、素材绑定、quota、batch/task/link 在同一事务内，任务冻结采用 SAVEPOINT，永久业务错误可只保留失败时点，未知内部异常回滚整个事务。模拟中途冻结异常没有孤儿 batch/task/link。
- 隐私与访问：Pool API 全部经过当前可访问 Page 范围；共享查看者只能读池状态，非任务所有人/管理员不能从该入口取得其他人的 caption、短链或任务错误细节。
- 晚返回：池状态只派生自已冻结任务，不反向复活任务；原任务租约与状态 CAS 继续保护取消后 GPU 回调、旧 lease 和未知 Graph 结果，未修改旧 Worker 发布/对账合同。
- 上线/回退：真实主程序只增加 identity 路由，旧列全部逐行指纹验证不变；既有短链文件/编号不在复制或恢复目标内。重新开放后失败保留兼容 backend 与 SQLite；功能回退只关闭新增入口和 scheduler，保留已冻结任务执行。

## 独立执行结果

| 检查 | 结果 |
|---|---|
| `scripts/test_fb_video_page_pool.py` | 40 项通过 |
| `scripts/test_fb_video_pool_identity.py` | 19 项通过 |
| `scripts/test_fb_video_publish_core.py` | 41 项通过，含冻结文案、取消/晚回调、unknown 与旧 lease |
| `scripts/test_fb_page_material_pool_deploy.py` | 8 项通过 |
| 原 gateway / auto service / 原 deployment 回归 | 9 / 2 / 7 项通过 |

截至本次复核，上述已知 P2 已关闭，未发现范围内其他可复现阻断项。该结论限于代码、真实主程序只读快照和隔离测试；正式服务器健康、权限 HTTP 验证、当前唯一真实成功任务与短链的前后指纹由主代理在部署阶段核验。
