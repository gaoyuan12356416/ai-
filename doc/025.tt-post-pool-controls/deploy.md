# 部署与回滚

## 范围

仅 CPU 端：

- `features/tt_posts/core.py`
- `features/tt_posts/service.py`
- `static/tt-post-pool.html`
- TT CPU sidecar release 与 AI 后台、Nginx 的页面副本

GPU worker、GPU release、COS、媒体制作 profile 和环境变量不变。

## 部署前

1. 从 GitHub 已推送的精确提交创建不可变 CPU release。
2. 核对 `/mnt/data-disk` 的挂载 UUID、空间和写权限。
3. 记录当前 release、服务/timer、正式门禁、排期、可用素材、队列、run 和 publish ID 基线。
4. 使用 SQLite online backup 备份生产库，并备份 current 指针、服务代码和两份公开页面。
5. 候选 release 运行 TT core/service/UI/app contract 回归及 Python 编译。

## 部署

1. 原子切换 `/opt/tt-post/current` 到精确提交 release。
2. 从同一提交同步 `tt-post-pool.html` 到 AI 后台静态目录和 Nginx 公共目录，并核对三份 SHA-256。
3. 重启 `tt-post-service.service`；本次 `app.py` 无变化，不覆盖共享 monolith。
4. 验证 CPU sidecar、主 API、runner/prepare 单元及 `127.0.0.1:18830` GPU 隧道。
5. 完成登录态只读验收；不得提交排期开关或触发真实发布。

## 回滚

1. 将 `/opt/tt-post/current` 原子切回部署前 release。
2. 恢复备份的 TT 页面到 AI 后台静态目录和 Nginx 公共目录。
3. 重启 `tt-post-service.service`，核对健康、数据库完整性和页面哈希。
4. 默认不恢复 SQLite：本次无 schema 变更，正常部署不会修改业务数据。只有确认数据库损坏且停掉所有 TT writer 后，才使用在线备份恢复。

## 生产记录

部署提交、release、备份路径、测试数量、文件哈希和验收基线在完成上线后补录。
