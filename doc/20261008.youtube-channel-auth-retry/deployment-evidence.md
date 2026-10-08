# Watch More Dramas 授权检查修复验收

验收时间：2026-10-08 14:45 北京时间。Watch More Dramas 已恢复
`auth_status=verified`、`eligible=true`，页面可选；创建任务前实时鉴权通过。

## 原因与最终行为

数据库中的最新授权更新于 2026-10-08 14:28:08。Token 刷新返回 HTTP 200，
授权 scope 完整。Google 频道接口间歇返回 HTTP 401 `global/authError`；
同一 Access Token 又曾返回 HTTP 200、匹配频道身份且 `longUploadsStatus=allowed`。
原检查把首次频道读取错误判为授权失败，并在频道快照中缓存 300 秒。

最终检查只对这个具体只读错误重试，单 Token 最多四次 GET、间隔 1/2/4 秒。
选频道时仍拒绝该 Token，则只再刷新一次，重新检查 scope 并重复有限读取；
最多两次 Token 刷新、八次 GET。持续失败显示临时检查不可用，继续禁止选用。
真正刷新失效、频道不匹配、权限不足、长视频资格及历史封面失败仍拦截。
发布前频道身份 GET 使用相同的有限读取重试。上传、公开设置及评论没有新增重试。

本轮证据支持接口间歇接受凭证；没有证据确定 Google 内部发生了哪种同步问题。
[Google 官方错误定义](https://developers.google.com/youtube/v3/docs/core_errors)
说明 `authError` 与请求认证凭证有关。

## 代码、服务器与验证

- GitHub：`gaoyuan12356416/ai-`，分支 `codex/youtube-channel-auth-retry-20261008`。
- 最终运行提交：`808da1f020dd08d74db6220787bb60af37aa9d74`，已 push，CPU fetch SHA 一致。
- 服务器：`43.166.187.96`；运行目录：`/root/drama_material_service`。
- 仅安装 `features/youtube_auto_publish/channels.py` 和 `features/drama_synthesis/youtube.py`。
- Linux 最终版本 162 项通过：新回归 11、频道缓存 19、共享客户端/流程 86、审核发布引擎 46。
  Windows 新回归/频道缓存 30 项通过，之前共享客户端 86 和审核引擎 46 项通过。
- Python 编译、`git diff --check`、部署前 live SHA 校验通过；API health HTTP 200。
- API active，PID `3455409`；auto worker 平滑切换 `1482318 -> 3450708`，active。
  原 inactive 旧 publisher 保持 inactive；统一 writer 保持 active，PID `2937249` 不变。
- 三次独立实时 probe 均 verified；创建前包含首评资格的 `validate` 通过。
- 真实现存授权会话 GET 公网 `/api/youtube-auto-publish/channels` 返回 HTTP 200；
  频道 284 的 checked_at 为 `2026-10-08T06:45:47Z`，verified/eligible。
- 业务表前后数量一致：发布账本 454、准备任务 452、自动归因 432、手动短链 124。
  全部既有发布 ID、视频/评论 ID、上传/首评尝试次数和 unknown_outcome 完全一致。
- 没有创建生产测试任务、视频、评论或短链。没有为了验收发布平台内容。
- AI 后台技能已新增并关联 `references/youtube-channel-auth-checks.md`。
  技能目录中的其他既有修改保留，未一并提交。

## 备份与精确回滚

两次安装均包含文件 SHA manifest 和在线 SQLite 备份；数据库备份 quick_check=ok。
SQLite 只用作证据，回滚禁止覆盖当前业务数据。

最终版本备份：
`/mnt/data-disk/deploy/youtube-auto-publish/backups/channel-auth-retry-20261008-144422-808da1f020dd`。

最初原代码备份：
`/mnt/data-disk/deploy/youtube-auto-publish/backups/channel-auth-retry-20261008-143943-c673026718e5`。

在 CPU 上按顺序执行，恢复本轮修复前代码：

```bash
python3 /mnt/data-disk/deploy/youtube-auto-publish/releases/808da1f020dd08d74db6220787bb60af37aa9d74/scripts/deploy_youtube_channel_auth_retry.py --rollback /mnt/data-disk/deploy/youtube-auto-publish/backups/channel-auth-retry-20261008-144422-808da1f020dd
python3 /mnt/data-disk/deploy/youtube-auto-publish/releases/c673026718e5d478aba08cef8cd2fc48da4d87f0/scripts/deploy_youtube_channel_auth_retry.py --rollback /mnt/data-disk/deploy/youtube-auto-publish/backups/channel-auth-retry-20261008-143943-c673026718e5
```

脚本拒绝覆盖后续文件漂移，保留所有当前数据库、任务、资产及发布事实。
API 精确重启；共享客户端改变时只向 auto worker 主 PID 发 SIGTERM，
等待现有制作结束并确认 systemd 新 PID 与 health。
