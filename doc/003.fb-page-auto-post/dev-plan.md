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
| desc/url 宏与短链 | Codex | validation、repositories、core、links、publisher、UI/Nginx | 本地实现与QA完成；closed-gate部署待执行（2026-08-20） |

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
