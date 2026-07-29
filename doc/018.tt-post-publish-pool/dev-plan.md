# 开发计划

## 开发范围

在现有 AI 平台新增 TT Post 发布池，CPU 负责业务面，GPU 负责数据盘成片和 TikTok API 面。真实 Direct Post 以三重环境门禁默认关闭。

2026-07-29 增量范围：在不新增 API 路由、数据库表或 GPU 服务的前提下，将单素材表单扩展为浏览器编排的 1–100 素材批量流程；增加首条时间加可编辑间隔、可编辑描述模板、逐项部分失败隔离、确定性 prepare 复用及旧请求兼容。

## 任务拆分

| 任务 | 负责人 | 文件/模块 | 状态 |
| --- | --- | --- | --- |
| 需求、规则和 API 设计 | PM/SA | `doc/018.tt-post-publish-pool/` | 已完成 |
| CPU SQLite 状态机与安全账号源 | 开发 | `features/tt_posts/` | 已完成并上线 |
| GPU 成片与 TikTok sidecar | 开发 | `features/tt_gpu/`、`scripts/tt_gpu_worker.py` | 上一基线已完成；本轮语义不变 |
| 主后台路由、权限和审计 | 开发 | `app.py` | 上一基线已完成；本轮不新增路由 |
| 发布池 UI 与导航 | 前端 | `static/tt-post-pool.html`、导航配置 | 已完成并上线 |
| claim/runner、systemd 与隧道 | 开发 | `scripts/`、`deploy/` | 上一基线已完成；本轮语义不变 |
| 单元、合同、浏览器与线上关闭态验收 | QA/SA | `scripts/test_tt_*.py` | 已完成，275/275 通过 |
| GitHub-first CPU/GPU 部署 | 运维 | immutable release | CPU 已上线；GPU 本轮未改 |

### 本轮增量任务

| 任务 | 文件/模块 | 完成条件 | 状态 |
| --- | --- | --- | --- |
| 批量素材 UI | `static/tt-post-pool.html` | X 同款分隔/去重、1–100、逐条 preview、结果列表、单项失败继续 | 已完成 |
| 批量排期 UI | `static/tt-post-pool.html` | 首条上海时间、1–1440 分钟间隔、默认 10 分钟、逐条 queue、失败项不影响后续 | 已完成 |
| 可编辑描述模板 | UI、`features/tt_posts/` | 默认模板首屏显示；占位符校验；每素材真实 ID 渲染；模板和最终文案冻结 | 已完成 |
| prepare 复用 | `features/tt_posts/service.py`、GPU 合同测试 | preview 与 queue 对同一素材身份使用同一确定性 job，源/profile 变化时失效 | 已完成 |
| 幂等与历史兼容 | Core、Service | 模板/文案纳入冲突比较；旧 `caption_text`、缺省模板及历史任务精确重放 | 已完成 |
| 自动化与浏览器验收 | `scripts/test_tt_*.py`、生产关闭态页面 | 新旧回归通过；混合失败、同时间冲突、多任务重试和历史重放通过 | 已完成 |
| GitHub-first CPU 部署 | CPU immutable release | 备份、切换、健康检查和公网浏览器验证；不创建真实 Post | 已完成 |

本轮未修改 GPU release 的媒体制作语义，也未新增 TT 批量表。CPU 已切换至 `/opt/tt-post/releases/5cfc657`；GPU 保持 `/opt/tt-post-gpu/releases/18148b2`。

## 编译 / 构建命令

```powershell
python -m compileall features scripts
python -m unittest discover -s scripts -p "test_tt_*.py" -v
```

增量测试还需覆盖：

- 1、100、101 项以及非法/重复素材 ID；
- 混合 preview 成功/失败与混合 queue 成功/失败，确认后续项继续；
- 首条时间、默认/自定义间隔、预览失败不占槽、建队失败后续不前移；
- 两项素材使用同一模板但渲染不同真实 Drama ID；
- UTF-16 2200/2201 边界、缺少/未知占位符；
- 精确批量重试、同幂等键改模板冲突和历史旧请求重放；
- preview/queue 的确定性 prepare job 复用。

浏览器验收使用线上 Cookie 会话打开 `/tt-post-pool.html`，验证权限、批量素材结果、模板编辑、时间序列、表单门禁和任务列表。生产关闭态验收不得开启三重门禁或创建真实 TikTok Post；如需验证建队，应使用负责人批准的安全测试数据并在验收记录中写明任务 ID 和清理/保留策略。

## 风险与依赖

- 生产主服务与 X sidecar 的已部署能力已在整合版本中无损合并并完成 93 项 X 回归。
- 依赖 CPU 的 63350 只读数据库连接和 GPU 的 `/data`、NVENC FFmpeg。
- 依赖 CPU↔GPU 的专用 SSH 反向隧道。
- TikTok Direct Post 的审核、Intended Use、品牌片尾和 URL Property 未确认，live gate 必须保持关闭。
- 快照 Token 无 scope 元数据，必须以 `creator_info` 实测为准。
- 批量逻辑位于浏览器，刷新页面不会产生服务端批次恢复；必须依赖每条稳定幂等键保证网络重试安全。
- 最多 100 个素材的 preview/queue 为顺序请求，整体耗时可能较长；页面已持续显示进度，且某项失败不会中断循环。
- 部分成功是明确产品语义，不是数据库整批事务；页面不得用“全部成功”文案掩盖失败项。

## 完成记录

- 2026-07-29：完成线上 X/TT 快照和 GPU 数据盘只读核验。
- 2026-07-29：确定 CPU/GPU 业务边界、数据结构、状态机和合规门禁。
- 2026-07-29：完成批量素材、时间间隔、可编辑描述、部分失败、prepare 复用和历史兼容实现。
- 2026-07-29：自动化测试 275/275 通过（TT 154、X 93、素材状态 28）。
- 2026-07-29 18:48:36 CST：CPU 切换至 `/opt/tt-post/releases/5cfc657`，GPU 保持 `/opt/tt-post-gpu/releases/18148b2`。
- 2026-07-29：公网 200/no-store、数据库完整性和 Chrome 登录态关闭态验收通过；三项 Direct Post 门禁保持为 0，未创建任务。
- 2026-07-29：确认保留既有 TT 个号设置原子批量保存能力，发布池仅只读消费已保存设置。
