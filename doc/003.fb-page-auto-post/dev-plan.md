# 开发计划

## 开发范围

隔离 sidecar、只读 MySQL、Graph 提交/对账、主 API 权限代理、两张页面、systemd、测试和文档。

## 任务拆分

| 任务 | 负责人 | 文件/模块 | 状态 |
| --- | --- | --- | --- |
| 模板/账本/租约 | Codex | `features/fb_auto_posts/core.py` | 完成 |
| Page/素材只读源 | Codex | `repositories.py` | 完成 |
| Token/Graph/对账 | Codex | `publisher.py` | 完成 |
| Sidecar/API代理 | Codex | `service.py`、`client.py`、`app.py` | 完成 |
| UI/导航/权限 | Codex | 两张 HTML、导航、权限 | 完成 |
| 部署/测试/文档 | Codex | deploy、scripts、doc | 完成 |
| desc/url 宏与短链 | Codex | validation、repositories、core、links、publisher、UI/Nginx | 完成（2026-08-20） |
| 北京自然日提前预制与版本门禁 | Codex | core、service、env、systemd运行配置、测试/文档 | 预制与live已部署；首个自然到点批次待观察（2026-08-21） |

## 编译 / 构建命令

```powershell
python -m py_compile features\fb_auto_posts\__init__.py features\fb_auto_posts\validation.py features\fb_auto_posts\repositories.py features\fb_auto_posts\links.py features\fb_auto_posts\core.py features\fb_auto_posts\client.py features\fb_auto_posts\publisher.py features\fb_auto_posts\service.py scripts\fb_auto_post_service.py scripts\fb_auto_post_runner.py app.py
python -m unittest scripts.test_fb_auto_validation scripts.test_fb_auto_repositories scripts.test_fb_auto_links scripts.test_fb_auto_store scripts.test_fb_auto_publisher scripts.test_fb_auto_service scripts.test_fb_auto_app_contract scripts.test_fb_auto_deploy scripts.test_x_accounts_app_contract scripts.test_tt_auto_publish_app_contract scripts.test_x_auto_publish_app_contract -v
node --check static\quick-nav.js
```

## 风险与依赖

生产依赖只读 MySQL、独立 service 用户/运行与指标数据盘文件、root-only env，以及实际资产 manifest/COS public-read/NVENC 集成。Graph v22.0 既有视频 status 已完成只读验证；真实发帖 live gate 未获批准不得开启。

## 完成记录

2026-08-17 开发阶段：本地候选完成；当时未提交、未推送、未部署。

2026-08-18 V2 开发阶段：按确认口径加入受控 Dramawave 映射、独立日指标缓存、未来 due-slot 调度、提前 GPU prepare、strict random_overlay、稳定 Page job ID、容量门禁与新 units。

2026-08-18 部署阶段：GitHub-first commit/push、CPU/GPU备份及closed-gate生产部署完成；生产只读MySQL、30日指标cache与prepare-only GPU/COS/NVENC canary通过。未调用Meta，live gate保持0。

2026-08-20 扩展阶段：先锁定 `{{desc}}`、`{{url}}`、`AIpost`、`/ads/0/2049/view` 契约，再实现加法列、批量描述读取、不可变 wrapper 与独立 Nginx 路由；测试、代码评审和 closed-gate 部署完成前不得改为“完成”。

2026-08-20 部署完成：GitHub commit `1b9fe57a90c9e64ab8ce05140fc6d0ed1d576c52` 已切到 `/opt/fb-auto-post/current`；仅重启 sidecar、reload Nginx并更新两份创建页，主 API 未重启。live gate与六张业务表保持0，未创建wrapper或Graph Post。

2026-08-21 日预制阶段：生产只读审计确认模板1 v2 enabled，但持久化总开关仍为0，当前不会自动发布；Page池62为13/8，总量语义为5批次×8 Page=40条/日。实现并验证 `FB_AUTO_PREBUILD_DAYS_AHEAD=1` 后再按备份、精确SHA、生产预建和ready门禁顺序切换。

2026-08-21 预制与live部署：release `af1c3b1` 已部署，代码切换前备份目录为 `/mnt/data-disk/fb-auto-post-deploy/backups/20260821T164759+0800-pre-af1c3b1`；先以 `FB_AUTO_PREBUILD_ENABLED=1`、`FB_AUTO_POST_LIVE_ENABLED=0` 生成明日完整5个时隙，计划快照为13个Page，其中8个可发布、5个缺Token。首条自动任务task 4/run 2于17:04:41开始、17:14:03 ready，成片58,144,303 bytes、SHA `cf2b71128cefd8c62d209fa23015195e73e532aa9487e9bbe02d96033075da46`、profile `tt-post-random-overlay-h264-720x1280-v3`，公开HEAD 200且元数据一致。17:14:48 CST在running=0/preparing=0/ready=1门禁下开启live，切换前两库备份位于 `/mnt/data-disk/fb-auto-post-deploy/backups/20260821T171448+0800-pre-live-af1c3b1`；health显示prebuild/live均为true，服务`NRestarts=0`。开启后4次execute均为no_pending、reconcile为no_submitted、attempt仍2、ledger仍1、early running=0。第二条自动任务task 6于17:14:51开始、17:16:46 ready，成片20,205,203 bytes，第三条自动任务已进入preparing，且attempt/ledger仍无增量。FB专项128/128、X/TT合并基线66/66通过；首个自然到点批次的最终Graph对账仍为未来观察项，不提前填写。
