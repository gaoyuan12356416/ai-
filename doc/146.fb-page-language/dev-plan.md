# 开发计划

## 开发范围

模板 UI 与保存契约、Page 语言路由、规划时快照与异常隔离、未来运行缺失 Page 扩充、独立 QA 和部署验收。

| 任务 | 负责人 | 文件/模块 | 状态 |
| --- | --- | --- | --- |
| Page 语言 helper 和计划器 | 主任务 | `features/fb_auto_posts/languages.py`、`core.py` | 实现已到位，独立测试通过 |
| 模板输入与 UI | 主任务 | `validation.py`、模板 HTML/JS | 已上线；未登录 DOM 通过，登录保存未执行 |
| 独立 QA 与文档 | QA 子任务 | 本目录、两套新增测试 | 39/39 通过 |
| 未来运行缺失 Page 扩充 | 主任务、QA | `pool_extension.py`、`scripts/test_fb_pool_extension.py` | 19 项通过；BUG-001 已关闭 |
| 全量 FB 回归 | 主任务 | 既有 FB 测试集 | 原 186 项证据保留；最新 192 项通过，23.191 秒 |
| 语言版本生产发布 | 主任务 | CPU 与静态资源 | `4201910` 已部署，HTTP/哈希/健康/历史不变读回通过 |
| 后续容量修正 | 主任务 | publisher 领取循环与部署配置 | `284f06e` 已部署，4 项测试通过（0.111 秒） |
| 145 Page 未来运行扩充 | 主任务 | `119..124`、操作 CLI | 17:19:48 读回新增 732，6×145唯一Page，原三表逐行不变 |
| 服务器巡检替代 Codex 自动任务 | 主任务、QA | watch 脚本、service/timer、新增测试 | heartbeat 已 PAUSED；17 项独立测试通过，20eb3a8 已部署，首次报告正常 |

## 验证命令

QA 实际执行并通过：

```powershell
python -m unittest scripts.test_fb_page_languages scripts.test_fb_pool_extension -v
```

主任务已报告完整 FB 回归通过；编译/JS 验证完整命令和生产证据由主任务补充。QA 子任务未重复执行完整套件。

主任务新增容量验证为 4 项、0.111 秒；不覆盖或替换既有 39 和 186 项回归证据。本次 QA 后续只更新文档并只读审查 CLI，未重跑测试或操作应用。

## 完成记录

2026-09-18：用技能的 `scaffold_requirement.py` 创建本目录；最终独立套件 39/39 通过，耗时 5.694 秒。扩充重试缺陷 BUG-001 已修复并回归。QA 未访问服务器，未发布真实内容。

同日补记 `4201910` 语言上线、`284f06e` 8 路持续发布容量修正，以及 CLI 必须持续暂停五类 timer、冻结缓存的维护窗口约束；旧 1511 tasks / 2245 attempts / 1205 ledger 部署前后不变。

最终应用 `90faff9` 已上线，HTTPS 修复 `f7cb58f` 生效；7 种语言各 500 候选冻结审核完成。732 条已 apply 且历史逐行不变，五业务 timer active，双路制作自然启动；今日新增 122 个 Page 为 21:30，旧 23 个为 18:54。新增只读巡检 17 项测试通过（0.014 秒），只用服务器有限 timer，不再启用 Codex heartbeat。
