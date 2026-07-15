# 测试报告

## 测试结论

本地安全边界、功能测试与生产回归全部通过；`ads_ai.ad_control_action_log` 已在 `63353` 创建，服务已部署，16条历史日志已幂等回填并通过真实登录态页面验证。

## 测试范围

执行批次、限流熔断、Graph安全门、续跑状态、MySQL日志适配、迁移保护、日志UI、静态缓存与共享app窄补丁。

## 执行统计

| 类型 | 数量 | 通过 | 失败 | 阻塞 |
| --- | --- | --- | --- | --- |
| Python unittest | 29 | 29 | 0 | 0 |
| Python compile | 5 | 5 | 0 | 0 |
| JavaScript syntax | 1 | 1 | 0 | 0 |
| 生产同源补丁演练 | 3 | 3 | 0 | 0 |
| 63353临时写探针 | 6 | 6 | 0 | 0 |
| 线上部署后回归 | 5 | 5 | 0 | 0 |

## 缺陷情况

BUG-001 及评审发现均已修复；未发现未关闭的 P0/P1。

## 验证证据

- `python -m unittest discover -s tests -p 'test_*.py' -v`：29/29。
- 生产app、runner、feature service、migration与部署补丁共5项Python编译检查：通过。
- `node --check static/ad-control-pages.js`：通过。
- 生产同源复合app临时快照：第一次patch=changed、第二次=unchanged、编译通过、所需函数齐全。
- 7个HTML：CSS/JS全部引用 `20260715log1`。
- `43.166.187.96 -> 101.32.56.53:63353`：`@@read_only=0`，`CURRENT_USER()=ads_aius@43.166.187.96`。
- 随机探针表完成 CREATE、单行 INSERT、UPDATE、SELECT、DELETE、DROP；操作全部成功，探针表已确认无残留。
- 运行时代码负向门禁：错误端点、错误库表、超512KiB、第三个突发写均在连接数据库前失败；读只走63350，写只走63353，运行时无DDL/DELETE。
- 首次日志写入回退SQLite时，runner不会立即再发状态UPDATE；仅`log_store=ads_ai`时允许第二条受控状态写入。
- API与runner的live preview跨账户并发均默认4，生产补丁会把旧默认12收紧为4。
- 正式表已在 `63353` 创建并从 `63350` 回读：31列、5个索引、InnoDB；上线单行写/更新/读取/清理探针通过且0残留。
- 生产源码为 `82fdab6a5c88565a45d1e7d2ac2a9dddf9bb3dce`，线上文件与该commit逐项SHA一致；备份目录为 `/root/drama_material_service/backups/ad-control-execution-log-20260715-130314`。
- SQLite与MySQL均有16个 action_id 且集合完全相同；同批迁移二次执行后仍为16行。
- `drama-material-api.service` 为active，部署后journal error为0；公网两个ad-control页面均返回200，未登录日志API返回预期401。
- 真实Chrome登录态显示16张日志卡；首条日志详情懒加载显示186/186条，原始结果计数为186。
- 首次部署因静态页健康检查使用了错误的后端直连路径而自动回滚；文件、环境与服务恢复均经校验，修正检查路径后的最终部署通过。

## 遗留风险

- 正式Meta pause不作为上线首测；先做日志读、preview/dry-run。
- `results_json` 后续需制定180天归档策略。
- 后续共享生产app部署仍必须对真实文件做备份和diff门禁。
- 当前 `ads_aius` 在 `ads_ai` 内仍有宽权限；应用和 skill 门禁不能替代未来由 DBA 创建的最小权限专用写账号。

## 发布建议

本次代码、建表、受控回填、服务验证和页面回归均已完成，可以发布。正式Meta pause未作为上线测试触发；后续功能验收继续先走live preview/dry-run，再按现有执行安全门操作。
