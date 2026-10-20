# 身份、运行时与定点集成验证

## 接口边界

- `GET /api/fb-video-publish/materials` 允许 `fbVideoPublish` 或 `fbPageManagement`，仅 `fbVideoPublishRecords` 权限不开放素材读取。
- `/api/fb-video-publish/pages/{page_id}/pool` 及其明列子接口仅允许 `fbPageManagement`；继续使用 Cookie、模块权限、导航权限、同源写入、4 MiB 上限和可信 actor。
- 启用计划在主 API 重新校验账号；sidecar 的 `PoolService.identity_resolver` 再次重新校验，后续每个时点由 `PoolScheduler` 使用同一解析器获取当前身份。不会回退为系统管理员发布。
- `POST /internal/fb-video-publish/identity` 仅接受 `{tenant_key,user_id}`，必须直接来自 IPv4/IPv6 loopback、唯一有效 Bearer，不得携带任何 `Forwarded`、`X-Forwarded-*`、`X-Real-IP` 头。Nginx exact location 对公网返回 404。
- 主 API 以只读 SQLite 参数绑定按 `tenant_key + user_id` 读取唯一账号；要求 `status=active`、有效 user/admin 角色，并调用主程序现有权限归一化、模块权限和 `fbPageManagement` 导航判断。邮箱映射仍通过现有 FIFO 只读 MySQL 查询，只接受唯一有效 `sub_user_id`。
- 身份响应只包含 `tenant_key,user_id,user_name,is_admin,business_owner_id,sub_user_id`。sidecar 客户端固定访问 `127.0.0.1:8787`，禁用环境代理和重定向，连接 3 秒、读取 35 秒、响应最多 16 KiB，无身份缓存。
- 失效账号、权限或归因分别为明确 403 `identity_inactive / identity_permission_denied / identity_attribution_invalid`；临时传输/响应异常为 503 `identity_unavailable`。sidecar 保留状态码并返回明确未接收，调度域按永久/临时错误处理。

## 创建人显示

`GET /pages` 只批量补齐缺失的历史 `created_by_name`，每批最多 200 个 ID，并限定当前租户。已有名称快照保留；账号不存在或读取不可用时回退 `created_by` ID；不读取或使用 `updated_by` 替代创建人。不写回、修改历史 Page 行。

## 运行与功能回退

- `FB_VIDEO_V2_ENABLED` 与 `FB_VIDEO_POOL_ENABLED` 在 runtime 默认均为 `0`，部署明确设 `1` 后启用。
- Pool scheduler 还要求 `FB_AUTO_POST_LIVE_ENABLED=1` 和 `FB_VIDEO_PUBLISH_ENABLED=1`。功能开关不会关闭现有 `manual_worker`，已冻结任务仍使用其原执行状态机。
- `fb-video-pool-scheduler.service` 使用现有 `fb-auto-post` 用户，执行 `scripts/fb_video_pool_scheduler.py`；只请求本机 sidecar `/internal/fb-video-publish/pool-start|pool-tick`，载荷仅 `worker_id`，不调用 Graph/GPU/MySQL。
- 每个 scheduler 进程生成一个随机 worker ID，约每 10 秒检查，单次 HTTP 最多 50 秒，SIGTERM 等待最多 60 秒。相同 worker ID 的 start 重试幂等；sidecar 重启后重新注册启动边界，domain 保证不追补启动前时点及 60 秒截止。
- 回退应停止/禁用此新增 unit，关闭上述两个新功能开关，恢复旧静态入口；保留兼容 backend、SQLite 增量结构、已有 task/link/pool/slot 事实和短链文件。不可恢复旧 SQLite 快照或旧 worker 覆盖已生成的 V2/pool 任务。

## 定点主程序补丁

`scripts/patch_fb_page_material_pool_integration.py` 的 `patch_app(text)` 仅在现有 V1 视频发布集成的 `DramaMaterialHandler.do_POST` 中，解析请求路径后插入 4 行 identity dispatch。不覆盖线上完整 `app.py`，不改原认证、查询 broker、公共接口和其他模块。已有标记必须匹配完整 dispatcher；重复执行无变化，锚点或已安装内容异常时拒绝。

本地只读线上快照：`output/pool-live-app-baseline.py`，原 SHA256 `4897589769737c9cc3a96cd7ff77182bf2ed5279d6970631fc86c1cb44fc4d3d`。测试验证应用补丁后移除新增 4 行即与原字节完全一致，并二次应用不变。该快照不进入 Git。

## 本轮验证

- `python -X utf8 scripts/test_fb_video_pool_identity.py`：19 项通过。覆盖同租户隔离、停用/角色/模块/nav 撤销、归因失效、SQL 标识注入、可信 actor 替换、转发头与重复 Bearer/长度拒绝、跨租户名称隔离、静态名称快照、pool 路由权限、真实主程序定点补丁、sidecar 分发、调度进程重试及真实临时 SQLite runtime 构造。
- `python -X utf8 scripts/test_fb_video_publish_gateway.py`：9 项通过。
- `python -X utf8 -m unittest scripts.test_fb_auto_service`：2 项通过。
- `python -X utf8 scripts/test_fb_video_publish_deployment.py`：7 项通过（原 FB 批量发布部署/回退回归，非新增素材池部署脚本验收）。
- 修改模块 Python 编译通过；上述验证不调用生产 Graph、GPU、短链或真实 SQL，不改生产队列。

正式上线、运行单元、真实 HTTP 拒绝/权限验证及功能回退证据由主代理在部署后追加。
