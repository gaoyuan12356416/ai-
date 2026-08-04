# 开发计划

## 当前状态

文档与合同已完成；业务代码、自动化、生产部署和实测待执行。本文不记录任何未实际运行的“通过”。

## 开发范围与任务拆分

| 任务 | 文件 / 模块 | 交付 | 状态 |
| --- | --- | --- | --- |
| D1 数据迁移 | `features/tt_posts/code_routes.py`, `core.py` | 幂等创建 `tt_post_code_route`、索引、公开 DTO 清理 | 待开发 |
| D2 code 分配 | `features/tt_posts/code_routes.py`, `core.py` | 安全随机、PK 碰撞重试、剩余槽兜底、全容量最早回收、queue 幂等 | 待开发 |
| D3 `{code}` 宏 | `features/tt_posts/core.py`, UI | 精确 token、preview 不分配、queue 一次渲染、UTF-16 校验 | 待开发 |
| D4 发布路由 | `features/tt_posts/links.py`, `service.py` | `c` / AppsFlyer 字段冻结、`af_channel=TT`、published 状态 | 待开发 |
| D5 Redis 缓存 | `features/tt_posts/code_routes.py`, `service.py`, env/deploy | `TT_POST_CODE_REDIS_*`、6381、read-through、故障旁路、陈旧 key 保护 | 待开发 |
| D6 公开 API | `app.py` / TT sidecar | `/api/public/tt-code/resolve`、元数据合并、source/错误/限流 | 待开发 |
| D7 新页面 | `static/tt-drama-code-search.html`, `.js` | code/ID 搜索、恰好五条 Featured、触摸/鼠标/按钮横滑 | 待开发 |
| D8 Nginx | `deploy/nginx/tt-drama-code-search.conf` | `/tt-code`、脚本和 API exact route；原 `/tt` 不动 | 待开发 |
| D9 自动化 | `scripts/test_tt_*` | core/service/app/UI/bridge/Redis/迁移/回归 | 待开发 |
| D10 文档回填 | 本目录 | 代码评审、缺陷、实际命令、部署与测试证据 | 待执行 |

## 推荐实施顺序

1. 在临时 SQLite 上实现加法 schema 与最新 published 查询索引。
2. 实现独立的 code allocator，并先覆盖 0%、碰撞、高占用和满容量模型测试。
3. 把 allocator 接入 queue freeze 事务和 `{code}` 一次渲染，保证失败全回滚。
4. 实现正式发布路由冻结与 published 状态转换，保持重试/unknown 保护。
5. 实现 Redis adapter；任何缓存异常都返回 SQLite 结果，不吞掉事实源异常。
6. 实现公开 API 和目标 URL 校验，再实现 `/tt-code` 页面。
7. 增加 Nginx/env/systemd 部署资产和全量回归。
8. 在数据库/Redis/静态副本完成部署演练；通过后才允许 GitHub exact commit 发布。

## 关键算法约束

### code 分配

- 事务开始后先按 `queue_id` 查幂等记录。
- 使用系统安全随机源生成四位 base36 code，以普通 `INSERT` 依赖主键判重。
- 随机重试次数有界；达到阈值且空间未满时，从随机起点确定性扫描空槽。
- 只有精确计数等于 `1,679,616` 时，按 `created_at, code` 删除最早记录并使用其 code 插入新记录。
- 不在 Redis 中决定唯一性，不使用 `INSERT OR REPLACE`。

### 最新 published clone

- 精确过滤 `content_id` 与 `status=published`。
- 排序 `published_at DESC, queue_id DESC`，只取一条。
- clone 后只改 `af_channel`，用统一 encoder 重建并校验目标；原行不更新。

### Redis 降级

- key 使用版本 namespace；仅缓存安全 DTO。
- miss/连接错误/超时/反序列化错误均读 SQLite。
- 回收替换后的 `DEL/SET` 任一步失败，旋转版本化 namespace 并熔断该缓存读路径，直到新 namespace/key 从 SQLite 安全刷新。
- Redis 停止期间 API 的 found/404/503 语义必须与启用时一致。

## 预计验证命令

以下命令须在代码完成后实际运行并把结果回填至 `test-report.md`，当前不声称通过：

```powershell
python -m py_compile app.py features\tt_posts\code_routes.py features\tt_posts\core.py features\tt_posts\links.py features\tt_posts\service.py scripts\tt_post_service.py
python -m unittest scripts.test_tt_posts_core scripts.test_tt_posts_service scripts.test_tt_post_links scripts.test_tt_posts_app_contract scripts.test_tt_post_pool_ui
node scripts/test_tt_drama_bridge.js
git diff --check
```

若新增独立测试脚本，应加入上述回归清单而不是只运行单个 happy-path。

## 风险与依赖

- 生产 Redis 当前需新建独立实例；配置、端口和权限必须先在候选环境验证。
- 全容量测试不得真实插入 1,679,616 行到生产；使用可注入小字符空间或临时 DB 模型证明算法。
- 新页面需真实触摸/鼠标浏览器验证，仅 DOM 字符串断言不足以证明不误触。
- 同期分支可能修改 `core.py`、`service.py`、`app.py` 和 Nginx；合并必须做语义评审。

## 完成记录

待代码、自动化和实测完成后补录 commit、变更文件、命令、结果、缺陷和回滚点。
