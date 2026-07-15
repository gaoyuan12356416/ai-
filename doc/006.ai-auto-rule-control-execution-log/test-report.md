# 测试报告

## 测试结论

本地安全边界与功能测试通过；`63353` 写节点探针通过，现网数据库表、服务与 API 尚未部署或变更。

## 测试范围

执行批次、限流熔断、Graph安全门、续跑状态、MySQL日志适配、迁移保护、日志UI、静态缓存与共享app窄补丁。

## 执行统计

| 类型 | 数量 | 通过 | 失败 | 阻塞 |
| --- | --- | --- | --- | --- |
| Python unittest | 29 | 29 | 0 | 0 |
| Python compile | 4 | 4 | 0 | 0 |
| JavaScript syntax | 1 | 1 | 0 | 0 |
| 生产同源补丁演练 | 3 | 3 | 0 | 0 |
| 63353临时写探针 | 6 | 6 | 0 | 0 |
| 线上部署后回归 | 5 | 0 | 0 | 5（尚未部署） |

## 缺陷情况

BUG-001 及评审发现均已修复；未发现未关闭的 P0/P1。

## 验证证据

- `python -m unittest discover -s tests -p 'test_*.py' -v`：29/29。
- 4个Python文件 `py_compile`：通过。
- `node --check static/ad-control-pages.js`：通过。
- 生产同源复合app临时快照：第一次patch=changed、第二次=unchanged、编译通过、所需函数齐全。
- 7个HTML：CSS/JS全部引用 `20260715log1`。
- 线上只读基线：旧 `ad_control_rule` 为0；唯一启用的是新规则组。
- `43.166.187.96 -> 101.32.56.53:63353`：`@@read_only=0`，`CURRENT_USER()=ads_aius@43.166.187.96`。
- 随机探针表完成 CREATE、单行 INSERT、UPDATE、SELECT、DELETE、DROP；操作全部成功，探针表已确认无残留。
- 运行时代码负向门禁：错误端点、错误库表、超512KiB、第三个突发写均在连接数据库前失败；读只走63350，写只走63353，运行时无DDL/DELETE。
- 首次日志写入回退SQLite时，runner不会立即再发状态UPDATE；仅`log_store=ads_ai`时允许第二条受控状态写入。
- API与runner的live preview跨账户并发均默认4，生产补丁会把旧默认12收紧为4。

## 遗留风险

- 正式Meta pause不作为上线首测；先做日志读、preview/dry-run。
- `results_json` 后续需制定180天归档策略。
- 共享生产app部署时仍必须对真实文件做备份和diff门禁。
- 当前 `ads_aius` 在 `ads_ai` 内仍有宽权限；应用和 skill 门禁不能替代未来由 DBA 创建的最小权限专用写账号。

## 发布建议

代码与 skill 推送 GitHub 后，部署仍需单独授权。按 `deploy.md` 完成维护窗口建表、受控回填、服务验证和 API 回归后，才可宣布线上功能完成。
