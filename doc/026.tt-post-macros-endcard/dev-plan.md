# 开发计划

## 开发范围

开发分为两个可独立验证和回滚的轨道：

- CPU/管理端轨道：`{url}`、`{desc}`、description 冻结、短链、SQLite migration、素材池 UI 与 queue caption。
- GPU 媒体轨道：新增 `direct_outro`、独立 profile、固定片尾合成、manifest/reuse 和 prepare-only 验收。

本轮已授权提交、部署及必要的服务重启；不授权保存或人为触发自动排期，也不创建真实 TikTok Post。部署期间只允许临时隔离 runner，结束时必须恢复原启用状态。

## 任务拆分

| 任务 | 负责人 | 文件/模块 | 状态 |
| --- | --- | --- | --- |
| DEV-01 单次宏 tokenizer、UTF-16 helper 与错误合同 | 后端 | `features/tt_posts/core.py` | 已完成并验证 |
| DEV-02 description additive schema、intake/pool/queue 冻结与幂等 | 后端 | `features/tt_posts/core.py` | 已完成并验证 |
| DEV-03 resolver 信任边界、入池传递、pool->queue 复制和最终渲染 | 后端 | `features/tt_posts/service.py` | 已完成并验证 |
| DEV-04 TT 短链 ID、W2A 参数、atomic wrapper | 后端 | `features/tt_posts/links.py` | 已完成并验证 |
| DEV-05 素材池 UI 宏帮助、逐素材 description 预览和 UTF-16 校验 | 前端 | `static/tt-post-pool.html` | 已完成并验证 |
| DEV-06 `direct_outro` mode/profile/eligibility | GPU | `features/tt_gpu/worker.py` | 已完成并验证 |
| DEV-07 复用已审核 Logo/tutorial-outro pipeline、asset fingerprint 和 reuse contract | GPU | `features/tt_gpu/worker.py` | 已完成并验证 |
| DEV-08 core/service/link/UI/GPU 自动测试 | QA/开发 | `scripts/test_tt_*.py` | 已完成并验证 |
| DEV-09 Nginx 精确短链路由和 TT 专用 COS 配置 | 运维 | `deploy/*` | 已部署并验证 |
| DEV-10 需求、评审、测试、API、部署、缺陷制品 | PM/SA/QA | `doc/026.tt-post-macros-endcard/` | 已完成并回填证据 |

以上任务均已完成代码评审、自动化测试、生产部署和 prepare-only 验收；详细证据以 `test-report.md` 与 `deploy.md` 为准。

## 实现顺序

1. 先完成 core 的 schema/migration、单次宏渲染和 UTF-16 helper，以此形成统一事实合同。
2. service 只从 resolver 获取 description，在 intake 冻结，并把值复制到 pool/queue；禁止请求体 description。
3. queue 在创建短链后一次性渲染完整 caption，失败时不得消费 pool 或落半成品 wrapper。
4. UI 使用 preview 响应逐素材展示，不提交 description，并回归 2c schedule 控件。
5. GPU 增加 `direct_outro`，将“可直发”和“是否需要片尾”拆成两个判断，避免复用现有 `direct_post_eligible()` 分支导致仍走 clean path。
6. 固定片尾和 Logo 资产参与 manifest/reuse；`direct_outro` 复用既有 Logo/tutorial-outro 命令合同，`direct_clean` 和 `branded_preview` 的命令数量、filter 和 eligibility 由原测试锁定。
7. 先执行离线测试，再在隔离环境做 prepare-only；最后才允许进入部署审批。

## 编译 / 静态检查命令

在仓库根目录执行：

```powershell
python -m py_compile features/tt_posts/core.py features/tt_posts/service.py features/tt_posts/links.py features/tt_gpu/worker.py
python -m compileall -q features/tt_posts features/tt_gpu scripts
```

## 自动测试命令

```powershell
python scripts/test_tt_post_links.py
python scripts/test_tt_posts_core.py
python scripts/test_tt_posts_service.py
python scripts/test_tt_post_pool_ui.py
python scripts/test_tt_post_prepare_runner.py
python scripts/test_tt_gpu_worker.py
python scripts/test_tt_posts_app_contract.py
```

若脚本支持 `unittest` 发现模式，可另执行：

```powershell
python -m unittest scripts.test_tt_post_links scripts.test_tt_posts_core scripts.test_tt_posts_service scripts.test_tt_post_pool_ui scripts.test_tt_post_prepare_runner scripts.test_tt_gpu_worker scripts.test_tt_posts_app_contract
```

## prepare-only 验证命令/证据要求

正式命令由部署环境的已有安全 wrapper 执行，文档不记录 token。需要保存以下证据：

1. GPU `/health` 的 `media_mode`、`profile`、`direct_post_eligible`、`transition` 和 storage health。
2. 唯一 job ID 的 `/internal/tt-post/prepare` 脱敏请求与响应。
3. `ffprobe -show_format -show_streams`、output SHA-256/size、源与成片 URL 对比。
4. 片尾首帧/末帧抽帧和人工观看结论。
5. 验收前后 queue、publish ledger、schedule 版本与 enabled 状态对比。

任何 publish、canary、run-now 或 schedule-save 命令均不得出现在本轮执行记录中。

## 风险与依赖

- 依赖 CPU 和 GPU 对 `direct_outro` profile 名称完全一致；切换顺序错误会 fail closed。
- 依赖固定片尾与 Logo 文件在 GPU 上可读、已审核，并有 SHA-256/size/duration 或尺寸记录和回滚副本。
- 依赖 TT 专用 COS 与验证域名可读；SecretID/SecretKey 只能走环境变量/密钥管理，不写入仓库和文档。
- 依赖 `ads_drama_resource.desc` 的唯一素材映射；多行和连续空白必须规范化，规范化后的空值或残留控制字符必须拒绝。
- 依赖 TT `s2l` 精确路由排在 X 通用路由之前。
- 共享分支中 2c 与宏/短链改动交叠，必须逐文件语义 review，并以最终工作区重新跑全量测试。
- 可变 source URL 会破坏 prepare 复用身份，需不可变 URL 或 source SHA/size。

## 完成定义

- 所有任务代码完成并通过 SA 最终 diff review。
- `test-cases.md` 的 42 条用例全部执行，`test-report.md` 回填真实证据。
- BUG-001、BUG-002 状态为“已修复并验证”。
- prepare-only 成片符合固定片尾和 Direct Post eligibility 合同，且确认没有创建真实 Post。
- CPU/GPU 回滚方案经过无外部发布的演练。

## 完成记录

- 2026-08-03：需求、SA、开发计划、测试矩阵、API、部署与缺陷文档已补齐。
- 代码最终评审、312 个 Python 用例、53 个 Node 断言、生产 migration/Nginx、环境 prepare-only、提交和部署：全部完成并通过。
- 部署 commit：`282eb914172531bd55500b65539d5715a282e5bc`；未创建真实 TikTok Post，自动排期与 gate 原状态已恢复。
