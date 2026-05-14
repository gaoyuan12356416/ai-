# 开发计划

## 开发范围

- 后端：配置、SQLite 表、产品查询、任务 CRUD、状态机、需求/素材生成适配、审核、最终素材上报。
- 前端：快速导航入口、任务管理页面、创建/编辑表单、任务详情、需求审核、素材预览和素材审核。
- 文档：需求、SA 评审、测试用例、API、部署、代码评审、测试报告。
- GitHub：新建分支、提交、推送、开 PR。

## 任务拆分

| 任务 | 负责人 | 文件/模块 | 状态 |
| --- | --- | --- | --- |
| 需求目录和文档 | Codex | `doc/001.AI投放素材制作/` | 已完成 |
| 后端表结构和服务函数 | Codex | `app.py` | 已完成 |
| 后端 API 路由 | Codex | `app.py` | 已完成 |
| 前端页面和交互 | Codex | `static/index.html` | 已完成 |
| 快速导航入口 | Codex | `static/quick-nav.js` | 已完成 |
| 环境变量示例 | Codex | `.env.example`、`deploy/drama-material-api.service` | 已完成 |
| 本地语法/状态机烟测 | Codex | `python -m py_compile app.py`、函数级烟测 | 已完成 |
| GitHub 同步 | Codex | git branch/commit/push/PR | 待执行 |

## 验证命令

```bash
python -m py_compile app.py
node --check static/quick-nav.js
node -e "parse static/index.html inline scripts"
```

另使用临时 SQLite 和临时素材目录完成函数级烟测：

- 创建任务。
- 生成需求。
- 需求审核通过。
- 生成 2 条素材。
- 逐条素材审核通过。
- 权限隔离、发布后禁止编辑、发布后允许复制、完成前允许删除。

## 实现顺序

1. 新增后端配置和表结构。
2. 新增产品权限查询，优先 MySQL，失败时降级到现有产品列表。
3. 新增任务和素材服务函数。
4. 新增 API 路由和权限校验。
5. 新增前端页面、导航、表单和详情交互。
6. 新增 `.env.example` 和 systemd 环境变量。
7. 运行语法检查、静态脚本检查和函数级烟测。
8. 整理代码评审和测试报告。
9. 提交、推送、开 PR。

## 风险与依赖

- GitHub 插件当前认证异常，PR 创建可能需要回退到本地 `git`/`gh` 或网页创建方式。
- CPU/GPU 真实生成命令需要线上环境配置 `AD_MATERIAL_REQUIREMENT_COMMAND` 和 `AD_MATERIAL_GENERATION_COMMAND`。
- 最终素材 API token 不能进入仓库，部署时写入 `.env` 或服务端环境变量。

## 完成记录

- 2026-05-14：已定位仓库 `D:\codex\ai-drama-material-service` 和远程 `gaoyuan12356416/ai-.git`。
- 2026-05-14：已创建分支 `codex/ai-material-task-management`。
- 2026-05-14：已完成需求文档、后端、前端、部署配置、测试用例、代码评审和测试报告。
