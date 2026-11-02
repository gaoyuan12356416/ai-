# 测试报告

## 测试结论

2026-11-02 部署前本地验收：47 个独立用例全部通过。真实 14 天回填、服务器 Python 3.9 运行、线上登录与性能、systemd 启用尚待部署阶段执行，不能据本报告认定线上已完成。

## 测试范围

实际查询和页面、源事实归一化、版本刷新/失败保留、权限与导航兼容、三表迁移解析、回滚拒绝无副作用、查询技能。

## 执行统计

| 类型 | 数量 | 通过 | 失败 | 阻塞 |
| --- | --- | --- | --- | --- |
| 导航与认证 integration | 7 | 7 | 0 | 0 |
| SQLite 查询与预计算 query | 17 | 17 | 0 | 0 |
| 归属、采集与刷新 data | 19 | 19 | 0 | 0 |
| Chromium 页面验收 | 2 | 2 | 0 | 0 |
| 迁移/回滚部署模拟 | 2 | 2 | 0 | 0 |

单独指定测试类，避免 UI 模块导入 QueryTests 造成重复计数；本次总耗时 8.075 秒。

## 缺陷情况

已修复：旧导航双模块权限放宽；孤立未发布快照可读；迁移第三表名称不一致；SQL COMMENT 内分号导致三表迁移解析失败；回滚被拒前提前停 timer；零收入未归属提示缺失；技能 UI YAML 的 GBK 编码。历史剧名读取改为按引用键获取，逐日控制量落入清单。详见 sa-code-review.md。

## 验证证据

```text
python -m unittest tests.test_socialkit_integration.IntegrationTests tests.test_socialkit_query.QueryTests tests.test_socialkit_analytics_data.AttributionTests tests.test_socialkit_analytics_data.CollectionTests tests.test_socialkit_analytics_data.RefreshTests tests.test_socialkit_ui.BrowserTests tests.test_socialkit_deploy.DeploymentTests
Ran 47 tests in 8.075s — OK
```

- 模拟迁移执行当前 SQL 文件，恰好 1 次 writer 身份 SELECT 和 3 条批准表 CREATE；不打开真实数据库连接。
- 实际 JS 导航迁移与后端结果对照，默认/自定义组、双模块 ACL、既有 SocialKit 自定义 ACL 保持；未登录、API token 和无模块授权被拒。
- 预计算只供同版本同默认参数使用，筛选不会误读首页；未发布版本返回 409，CSV 固定版本。未知日期为 null、成功零日为 0。
- 真实 Chromium 使用本地 fixture API 验证切页、筛选、图表、CSV 版本及零收入未归属提示，无页面 JS 错误。
- Python 3.9 grammar 对本功能 10 个 Python 模块通过；这是语法检查，真实解释器与 systemd 仍待服务器验证。两个新增/修改 JS 的 `node --check` 通过。
- 技能仓库及安装副本 quick_validate 通过；全部文本 UTF-8 解码及 YAML 精确 display_name 校验通过。通过本地模拟 SSH 传输运行实际 query 模块，JSON 与 CSV 同为 v1，收入 18.99、CSV 2 组；未访问生产源库。

## 遗留风险

待实测：真实源库 14 日期控制总额与维度守恒、2 秒响应目标、真实账号模块授权、CPU 部署及回滚锁、定时器北京时间下一次触发。源收入和发布日期口径不同，不可据本地合并展示推算单帖 ROI。

## 发布建议

可进入 GitHub 精确提交部署及首次回填；全部 14 日期验证完成后再启用日调度。线上执行结果应追加到本报告，保留本地与真实环境证据的区分。
