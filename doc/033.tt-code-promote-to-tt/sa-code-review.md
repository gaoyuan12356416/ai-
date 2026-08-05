# SA 代码评审

## 结论

通过。独立审计、完整相关回归和全仓基线均未发现本需求引入的 P0/P1/P2 问题，可以进入 GitHub-first 发布。

## 评审范围

- `/tt` 与 `/tt-code` Nginx exact location 的唯一性和安全头一致性。
- 旧静态文件、v1 resolver、v1 Featured 与旧 JS 路由的回滚完整性。
- Node/Python/浏览器测试是否以 `/tt` 为主入口且保留兼容入口。
- `{code}` 从素材模板、正式 queue 事务冻结、GPUClient 到 TikTok title 的数据流。
- 生产只部署单配置文件的 GitHub-first 与回滚方案。

## 检查结果

| 编号 | 级别 | 检查项 | 结论 |
| --- | --- | --- | --- |
| CR-001 | P0 | 是否重复定义 `/tt` | 否，合同测试固定为一次 |
| CR-002 | P0 | 是否覆盖线上旧 JS | 否，旧静态文件有零 diff guard |
| CR-003 | P1 | 是否引入页面代码复制 | 否，双入口 alias 同一 HTML |
| CR-004 | P1 | 是否触碰发布服务运行版本 | 否，只有测试增强，无运行时代码变更 |
| CR-005 | P1 | `{code}` 是否可能发送为字面量 | 正式队列测试断言冻结后无残留，GPU payload 原样一致 |
| CR-006 | P1 | 直接测试是否错误占用 code | 否，409 且不创建任务 |

## 验证结果

- TT Post 正式发布相关：237/237 通过；其余 TT UI/runner/app 合同：83/83 通过。
- Featured/resolver/Nginx Python：43/43 通过。
- 新/旧 bridge Node：155/155、53/53 通过。
- 真实 Chrome：30/30 通过，覆盖 `/tt`、`/tt-code`、桌面、390×844、简中、繁中、RTL、回退、拖动和拦截点击。
- 全仓 tests 基线：482 项，478 通过、3 个既有失败、1 跳过；既有失败仍为 `test_ad_control_v3_routes` 的 GET/POST/DELETE 顺序断言，本次未修改 `app.py` 或该模块。
- `compileall` 与 `git diff --check` 通过。

生产部署后仅需补充配置哈希、备份路径、精确提交与线上验收证据。
