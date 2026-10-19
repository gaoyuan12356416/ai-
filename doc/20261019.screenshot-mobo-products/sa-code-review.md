# SA 代码评审

## 结论

2026-10-19：离线实现与生产组合补丁审查通过，无产品功能阻断项。允许执行已批准的窄范围部署和两产品真实验收；完成真实图片/素材库回读前不认定生产验收完成。

最新结论（真实验收后）：上游 ai-source 对两个新产品返回 HTTP 200 / code=-1 / 素材 app_package 不支持导入，生产上传验收未通过，新产品自动化不得激活。下述回调业务码修复已独立复核通过；早期离线通过不代表上游支持已完成。

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

## 后续验证修正

真实只读核对确认素材回调会将图片重新存储至新的 COS URL。验收改为完整素材名（含 job 前缀及比例）+content_id+product 精确匹配，并下载入库文件比较 SHA256。首组原图遇到上游内容审核时保留失败，使用另一部普通海报独立验证，不改审核配置。部署 AST 指纹已补跨 Python 空字段和 bytes 常量回归（总计23项）。

## 回调业务拒绝与假完成修复复核

2026-10-19：review-only，未修改实现、未调用真实生成/上传、未重试任何历史任务。

- `post_screenshot_ai_source_callback` 现在要求 HTTP 2xx、可解析 JSON 对象且 code 为 0；code=-1、缺少 code、布尔值、数组、非 JSON 和 HTTP 302/403 均失败。禁用回调、缺少配置或三图不齐也不能标记入库成功。
- 真实 `process_screenshot_job` 在通知回调成功之后才写 done；回调异常由 `run_screenshot_job_async` 写 failed 和错误详情，保留 assets、三个输出 URL 及原 job_id。失败任务的人工 retry 继续走已有产物复用，不触发 done 分支的强制重制。
- 独立离线执行 AST 提取的真实生成后半段、真实 callback/notify 和真实异步错误处理，在 JOB_AUTO_RETRY_ATTEMPTS=1、HTTP 200/code=-1 场景下确认：只发生一次 mocked POST、记录 callback_screenshot_ai_source_failed、终态 failed、从未写 done、assets 和三个 URL 完全保留。
- `recover_inflight_screenshot_jobs` 仍排除 done/failed，runner 全状态去重仍跳过历史失败任务；本次 callback 修改没有新增历史重跑或自动重试入口。DramaWave 的消耗>=1000 且 AF D0 ROAS>=45 配置和筛选代码未改。
- 再运行 `python scripts/test_screenshot_mobo_products.py`：23/23 PASS；`python scripts/test_screenshot_model_compatibility.py`：5/5 PASS；`python -m py_compile app.py scripts/test_screenshot_mobo_products.py`：PASS。
- 当前阻断为上游产品包名白名单/导入支持，不能用禁止短剧的通用 source 接口绕过。继续保留成图和任务记录；仅纠正本次明确 canary 的假 done，不扩大历史状态变更范围。上游支持修复、素材记录回读成功前不得执行 activate。

### 两条 canary 定向纠正安装器

- `repair_callback.py` 只替换 callback 函数，要求完整 live app 哈希匹配并校验其他顶层函数不变；代码可编译。持共享锁、无在途任务后备份代码、部署状态、目标任务/原回执和整个 SQLite，API 重启后核对生成 worker PID 不变。
- 状态纠正只允许固定的两个 job_id、对应 app_id、剧 67767322、当前 done、原回执 code=-1 且包含 app_package 拒绝，并先从素材库确认这六个完整素材名不存在。事务内再次核对 done 和三个 URL，UPDATE 只改状态/进度/错误/更新时间，不改 assets、URL、job_id 或素材标识；每条写独立审计，outbound_requests=0。
- 保留首次完整回滚备份，仅更新当前部署代码哈希和 patch 记录；不激活 cron、不调用重试 API、不进行生成或上传。新增审计 INSERT 与现有表默认字段兼容。
- `python -m py_compile ops/screenshot-mobo-products-20261019/repair_callback.py`：PASS。仅静态审查，未执行 preflight/apply；实际修正后的两条记录仍须发布负责人回读确认。
