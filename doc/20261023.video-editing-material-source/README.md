# 剪辑成片手动入库

## 行为与边界

- 「制作任务」中只有已完成且有校验成片的任务显示「上传至素材库」。每次点击只处理这一条；历史任务不自动补传。
- `POST /api/video-editing/tasks/{task_id}/material-source` 只接受登录用户的剪辑权限、同源 JSON 请求和任务归属，浏览器不传 COS URL。响应 `item` 是该成片上传账本的当前状态。`GET /api/video-editing/tasks` 和 `GET /api/video-editing/tasks/{task_id}` 的 `material_upload` 用于刷新状态。
- 原成片对象保持私有。独立 worker 将其复制到 `advertising-1306474899` 的固定 `video-editing/material-source/v1/` 路径，检查源与副本大小、SHA 元数据、COS CRC（存在时）和匿名 HTTPS HEAD。小于 5 GiB 使用单次 COS 复制，大于等于 5 GiB 显式分块复制；单成片上限 8 GiB。线上 SDK 1.9.44 在同地域场景可能对大文件仍选单次复制，因此不能依赖其高级 `copy` 自动切换。
- 后端固定目标 `app_id` 为 JSON 整数 `3214`、分类为 `dramawave_post`、设计人员 `user_id=248`。`initiator` 必须由点击人邮箱唯一映射到有效人员 ID；邮箱无映射或有多个有效人员 ID 时明确返回 409，不猜测。原剧 `content_id` 和唯一国家/语言从只读剧目目录确认。签名按成片 ID 与 SHA 冻结，正式素材按签名读取，并严格核对产品名、视频类型、剧 ID、分类、URL、名称、人员和语言。接口 `data.id` 是回执 ID；它还需通过 `ads_source.source_id` 指向同一条正式 `ads_custom_source.id`，后者才是页面显示的素材 ID。
- 出站调用前持久记录尝试边界。超时、进程中断、缺回执、重复签名和正式行不匹配均进入 `unknown_outcome`，worker 只读对账，不再次 POST。明确拒绝记为 `failed`；重复点击只返回原状态。无效元数据、COS 复制失败和公开可读性失败不会向素材接口提交。

素材接口 2026-10-20 的新契约（`ai-cover-custom-source` 发布记录）记录 `content_id` 到正式 `data_source_id` 的回读规则，原始 `api-post-material-source.md` 已过时。目标产品 3214 的带 `content_id` 请求在本次上线前没有真实 POST；首次用户点击后仍须按正式行回读确认，不能以其他产品的成功样本代替验收。

## CPU 限域发布

仅从当前线上视频剪辑版本 `fd85308518c49678e5f62a8fee6cd7b886212499` 派生的干净、已推送 GitHub 提交部署。CPU 正式目录是复合安装，不能覆盖整个目录；GPU 不涉及此功能。发布前核对数据盘 UUID `3e8ac4e8-7770-456d-9e89-2ec5dd405fa8`、原九个目标文件与基线 Git blob 的 SHA256、独立 worker 单元尚不存在，并确认代码中的 `.env` 示例不包含凭据。

在 CPU 上将确切提交从 GitHub 拉到数据盘上的独立 release checkout 后，运行：

```sh
export GIT_SSH_COMMAND='ssh -i /root/.ssh/github_codex_cpu_ed25519 -o IdentitiesOnly=yes -o BatchMode=yes'
python3 scripts/deploy_video_editing_material_source.py
python3 scripts/deploy_video_editing_material_source.py --apply
```

脚本要求 HEAD 与 GitHub 分支 `codex/video-editing-material-source-20261023` 的已推送提交完全一致，只安装 `features/video_editing/{routes,service,store,material_source}.py`、新独立 worker 脚本和 systemd 单元、两份 `video-editing.{html,css,js}`、以及 `/etc/video-editing/video-editing.env` 中五个非密钥配置。它在数据盘备份旧文件、原始配置和在线 SQLite 快照；先启动 API 和新 worker，验证后才公布按钮。原剪辑 worker、其他任务 worker、YouTube 发布服务的状态必须保持不变，并记录进程 ID。脚本不恢复或重写现有任务/成片账本。

发布回读：核对 `systemctl is-active drama-material-api.service video-editing-material-source.service`，未授权 `GET /api/video-editing/config` 返回 JSON 401，六个静态目标文件字节哈希匹配提交；公网三份静态文件返回 200。只读检查新增 `ve_material_upload` 表为空、原 `ve_task`/`ve_output` 数量和关键行不变。真实素材接口 POST 留待用户首次点击。

## 回滚与首次验收

如需立即停止新的入库点击，在 CPU 创建 `/etc/video-editing/material-source.disabled`。它只拒绝新账本，不停止已有任务的只读对账。无任何 `ve_material_upload` 行时，可用安装输出的 `manifest.json` 完整恢复本次文件：

```sh
python3 scripts/deploy_video_editing_material_source.py --rollback /mnt/data-disk/video-editing/backups/material-source-<UTC时间>-<提交前12位>/manifest.json
```

已有入库账本行时，脚本会拒绝完整代码回滚并关闭新入库；保留后端、worker、SQLite 和 COS 对象，先对账。若只是页面需要撤回，恢复备份中的六个静态文件，后台继续处理已经被点击的任务。任何回滚均不得还原旧 SQLite 快照或删除原成片。

首次实际点击的验收记录应包含：任务/成片 ID、公开 COS 固定 URL 的匿名 HEAD 200 与大小、独立副本 SHA/CRC、素材接口回执 `data.id`、按 `content_sign` 读取的正式素材 ID，以及产品 `Drama-社媒专用素材`、`type=2`、原剧 `data_source_id`、`category=dramawave_post`、人员、语言、URL 的一致性。若接口对目标产品未保存剧 ID，状态必须保留 `unknown_outcome`，联系接口维护方按原签名修复；不得用新签名再次 POST 或直接改正式库。
