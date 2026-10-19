# SA 代码评审

## 结论

2026-10-19：离线实现与生产组合补丁审查通过，无产品功能阻断项。允许执行已批准的窄范围部署和两产品真实验收；完成真实图片/素材库回读前不认定生产验收完成。

## 评审范围

- `features/screenshot_products.py`：封面专用注册、上传 package、原封面过滤及确定性排序。
- `app.py` / 截图页面：封面权限、单条和批量创建入口、旧产品/视频产品隔离。
- 自动 runner / wrapper：阈值、北京日期、去重只读 fail closed、分产品错误隔离与退出码。
- 发布 diff：保持生产已有其他功能；不改变图像模型、并发、历史任务和既有图片。

## 问题清单

| 编号 | 严重级别 | 文件/位置 | 问题 | 建议 | 状态 |
| --- | --- | --- | --- | --- | --- |
| CR-PLAN-01 | P0 检查项 | runner / existing_jobs | 原实现读取异常返回空字典，会重复提交 | 已用 mode=ro + query_only、分块查询、finally close；缺失/损坏/缺表零提交 | 已验证 |
| CR-PLAN-02 | P0 检查项 | cover validator / resource SQL | 原查询要求分集，Moboreels 无分集不能使用 | 仅新产品 metadata-only；updated_at/id 确定性排序、封面和元数据同一行 | 已验证 |
| CR-PLAN-03 | P0 检查项 | runner profile | 共享默认阈值易改变 DramaWave 或污染新产品 | 固定 AUTO_PROFILES 覆盖旧环境阈值，新产品 SUM(spend)>3000，无 ROAS gate | 已验证 |
| CR-04 | P1 发布约束 | composite deploy | 生产有大量仓库不存在的功能，整文件覆盖会回退其他业务 | 使用带基线校验的 compose_app，只替换两个函数、增加一个 GET 分支 | 组合快照全 AST 验证通过 |
| CR-05 | P2 建议 | deploy / rollback | 独立 rollback 入口未取 cron 共享锁；apply 触发的自动回滚仍持原锁 | 独立 rollback 也持锁，避免定时任务提交窗口 | 已修复并静态复核 |
| CR-06 | P3 建议 | deploy / read_db 调用 | sqlite connection 的 with 只控制事务，不关闭连接 | 用 contextlib.closing 包装部署检查/备份数据库读取连接 | 已修复并静态复核 |

## 编译 / 验证结果

- `python scripts/test_screenshot_mobo_products.py`：22/22，PASS。
- `python scripts/test_screenshot_model_compatibility.py`：5/5，PASS。
- `python -m py_compile features/screenshot_products.py scripts/auto_submit_cover_synthesis.py scripts/test_screenshot_mobo_products.py ops/screenshot-mobo-products-20261019/deploy.py app.py`：PASS。
- 生产快照 `D:\codex\artifacts\screenshot-mobo-products-20261019\before\app.py` 通过 deploy.compose_app 组合与 compile；将新增路由移除、两个替换函数恢复后，完整 AST 与快照完全一致，证明生产独有代码被保留。
- 实际生产与仓库的 run_mysql、submit_screenshot_job/batch、fetch_drama_episodes、normalize_cover_source_url、product_name_for_app_id、post_screenshot_ai_source_callback、notify_screenshot_ai_source_callback、screenshot_material_id AST 相同，新 validator 调用签名及返回数据兼容。
- 生产原 URL 规范化只有 strip；本次保留该语义，不新增未经需求授权的协议过滤。原封面可下载、成图尺寸与入库归属由真实验收核对。

## 分阶段部署与真实验收脚本评审

- `--apply` 保留原单产品 wrapper，只上线 API/新 runner；`--activate` 在两产品真实验收通过后才替换三产品 wrapper。未改 cron 的 10:00、22:00 时刻。
- acceptance 的两个 canary 先落盘 intent，每条仅一次 POST；异常标记 unknown_outcome 后停止，manifest 存在时拒绝重新提交。单条和批量各验证一个新产品。
- check 按 app_id + content_id 精确回读唯一任务及 job_id，检查 done、callback 200 和业务 code=0，六张公网图片解码和三个实际尺寸；素材库按图片 URL、剧 ID、产品及图片类型验证六条记录。
- activate 要求部署 commit 一致、accepted=true、两个正确 app_id、六图和六入库记录，持共享锁后核验 wrapper 哈希再原子替换。失败时不启用新产品自动流程。
- 脚本已静态审查，QA 未执行真实 submit/check/activate；实际回执、图片及素材库证据由发布负责人写入 test-report。
