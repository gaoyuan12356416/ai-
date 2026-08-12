# 测试用例

## 测试范围

覆盖新模板全链路、X sidecar 桥接隔离、权限/敏感信息、调度幂等、选材规则、既有 X 发布回归和部署安全。

## 测试数据

- 临时 SQLite 与伪造 X 账号/候选/指标/sidecar 响应。
- 所有 X HTTP 写请求使用 mock，测试不得连接真实 X。
- 浏览器契约通过静态 DOM/JS 和本地 mock API 验证。

## 用例列表

| 编号 | 场景 | 预期结果 | 优先级 | 状态 |
| --- | --- | --- | --- | --- |
| TC-001 | 创建模板 | 默认停用，版本 1，配置哈希稳定 | P0 | 通过 |
| TC-002 | 编辑/并发版本 | 生成新版本；旧 expected_version 返回 409 | P0 | 通过 |
| TC-003 | 复制模板 | 配置复制但模板停用、身份独立 | P1 | 通过 |
| TC-004 | 固定/随机计划 | 北京时间稳定、随机不整点、重启不重抽 | P0 | 通过 |
| TC-005 | 启用/停用竞态 | 停用或版本变化后的陈旧 tick 不建 run | P0 | 通过 |
| TC-006 | 立即执行幂等 | 相同模板版本和 key 只产生一个 run | P0 | 通过 |
| TC-007 | 规则校验 | 语言、宏、范围、时长、类型和计划边界拒绝准确 | P0 | 通过 |
| TC-008 | 两层选材 | 剧/素材范围、排序、冷却和稳定 tie-break 正确 | P0 | 通过 |
| TC-009 | 指标缺日 | 失败关闭，不回退旧窗口 | P0 | 通过 |
| TC-010 | X 合规/历史占用 | 违规、映射错误、现有 pool/queue 素材被排除 | P0 | 通过 |
| TC-011 | 全局防重竞态 | 只有一个任务进入既有 `x_post_queue` | P0 | 通过 |
| TC-012 | >140 秒账号路由 | 仅当前 token 合格账号可选；降级后最终发布前失败 | P0 | 通过 |
| TC-013 | bridge 隔离 | manual claim 不领取 `auto_template`；自动 claim 不领取 `manual` | P0 | 通过 |
| TC-014 | 现有人工发布默认值 | 未传新字段时仍读取素材池正文且来源为 manual | P0 | 通过 |
| TC-015 | 模板闸门关闭 | scheduler/runner 自然执行无 run/task/queue/log/Post 增量 | P0 | 离线、生产通过 |
| TC-016 | unknown outcome | 停止后续，重复执行不再调用 X | P0 | 通过 |
| TC-017 | Cookie/权限/同源 | 401/403/404 和审计行为正确 | P0 | 通过 |
| TC-018 | 敏感信息 | API、DOM、日志不含 token、bearer、源媒体 URL | P0 | 通过 |
| TC-019 | 既有素材池/剧集池排期 | 相关现有测试全部通过且默认输出不变 | P0 | 通过 |
| TC-020 | 增量迁移 | 旧行摘要不变，重复迁移幂等，integrity_check=ok | P0 | 离线、生产通过 |
| TC-021 | canonical 预检失败 | 无 queue 时先记录 failed_preflight，再释放临时素材和账号；记录失败则重试 | P0 | 通过 |
| TC-022 | exact recovery | queued/no-log、reserved、publishing、锁忙和迟到线程均 fence，publish 最多一次 | P0 | 通过 |
| TC-023 | 关闭 live gate 后对账 | pending 不领取；已有 queue/run 只 reconcile，不 publish | P0 | 通过 |
| TC-024 | Linux 共享 flock | existing daily 持锁时 x_auto execute 必须跳过 | P0 | 生产通过 |
| TC-025 | 跨进程响应丢失 | 8810 响应丢失/重启后 busy 收敛到终态，publish 计数始终 1 | P0 | 离线通过；未做生产故障注入 |
| TC-026 | 生产 Chrome 页面样式与权限门 | CSS 200 且有规则；已登录管理员不显示登录/无权限提示 | P0 | 生产通过 |
| TC-027 | 模板/运行 DTO 显示 | 最近/下次执行、准备时长、任务状态、摘要和错误中文均读取真实字段 | P1 | 离线契约通过；生产空态与错误映射通过 |
| TC-028 | 静态缓存升级 | HTML `no-store`，CSS/JS 带统一 cache-buster；普通 reload 后不再复用旧脚本 | P1 | 生产通过 |
| TC-029 | 共享锁目录生命周期 | 多轮 X auto 与既有 X oneshot 后目录存在且 inode 不变 | P0 | Linux、生产通过 |

## 回归范围

- `features/x_accounts` OAuth/verify/soft-disable/publish approval。
- `features/x_posts` material/drama/schedule/manual/queue/log/media repair。
- `app.py` TT auto routes、X pool routes和未知 PUT/POST/GET 分发。
- quick-nav 用户权限和现有页面链接。
