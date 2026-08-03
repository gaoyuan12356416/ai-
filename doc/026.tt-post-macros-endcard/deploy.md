# 部署文档

## 当前状态

**执行中，待主任务回填。** 本轮按 GitHub-first 在最终 review 和测试通过后形成可追溯 commit，再以只读 release 目录部署；禁止直接在服务器 current 目录手改。允许部署与 `prepare-only`，不创建真实 TikTok Post，不保存或人为触发自动排期。

## 变更内容

### CPU/管理端轨道

- caption 支持精确 `{desc}`、`{url}`，单次非递归渲染和 2200 UTF-16 fail-closed。
- description 从 `ads_drama_resource.desc` 在 material intake 冻结，复制到 recurring pool/queue。
- queue 冻结 TT 19 位 `8` 开头短链，发布前原子写 W2A wrapper。
- SQLite additive migration、新 partial unique index、X 风格 TT 素材池 UI 与 2c 回归。
- Nginx 增加 TT 精确 `s2l` 静态路由。

### GPU 媒体轨道

- 新增 `direct_outro` 和 HEVC/H.264 独立 profile。
- 复用已审核 Logo/tutorial-outro + `phone-match-0.9s` 合成器，同时报告 `direct_post_eligible=true`。
- manifest v4 冻结 mode/profile/source/outro/logo/trim/transition；`direct_clean` 和 `branded_preview` 不变。
- 成片只上传 TT 专用 COS。

两个轨道必须能独立回滚。迁移和 profile 切换不能与真实发布同时进行。

## 配置项

### CPU `/etc/tt-post.env`

```dotenv
TT_POST_DB_PATH=/mnt/data-disk/tt-post-publisher/tt-post.sqlite3
TT_POST_SHORT_LINK_ROOT=/mnt/data-disk/tt-post-publisher/s2l
TT_POST_DEFAULT_SOURCE_TRIM_TAIL_SECONDS=0
TT_POST_MEDIA_PROFILE_VERSION=tt-post-direct-outro-hevc-720x1280-v1
```

若 GPU 选 H.264，则 CPU profile 必须改为 `tt-post-direct-outro-h264-720x1280-v1`。CPU/GPU profile 不一致时应 409 fail closed。

### GPU `/etc/tt-post-gpu.env`

```dotenv
TT_POST_GPU_MEDIA_MODE=direct_outro
TT_POST_GPU_VIDEO_ENCODER=hevc_nvenc
TT_POST_DEFAULT_SOURCE_TRIM_TAIL_SECONDS=0
TT_POST_GPU_FIXED_OUTRO_PATH=/data/tt-post-publisher/assets/TT-new-outro.mp4
TT_POST_GPU_LOGO_PATH=/data/tt-post-publisher/assets/dramawave-logo-rounded.png
TT_POST_GPU_STORAGE_BACKEND=cos
TT_POST_GPU_COS_BUCKET=socialkit-1306474899
TT_POST_GPU_COS_REGION=ap-hongkong
TT_POST_GPU_COS_DOMAIN=https://socialkit-cdn.yingliang.tech
TT_POST_GPU_COS_PREFIX=tt-post-prepared
```

`/etc/tt-post-gpu.secrets`（`root:root 0600`）只保存 SecretID/SecretKey、内部 token 和 seal key。仓库、部署日志、命令历史与本文件不得出现密钥值。

此 COS 是 TT Post 专用：GPU 制作完成后上传此桶，再用该 COS URL 进入 TT 发布流程；非 TT Post 业务继续使用原来的桶。香港机器可通过 COS 原生域名走内网，但对 TikTok 返回的 pull URL 必须使用已经完成平台验证的 `https://socialkit-cdn.yingliang.tech`。

### 短链/Nginx

```dotenv
TT_POST_SHORT_LINK_ROOT=/mnt/data-disk/tt-post-publisher/s2l
```

将 `deploy/nginx-tt-short-domain-location.conf` 放入现有 `gy.g2flow.com` TLS server，并置于 X 通用数字 `/s2l` location 之前。精确形状：

```nginx
location ~ "^/s2l/8[0-9]{18}[.]html$" {
    root /mnt/data-disk/tt-post-publisher;
    try_files $uri =404;
}
```

实际部署使用仓库文件中的完整安全 headers/CSP，不使用上面的节选替代。

### Gate 与排期

- prepare 不需要打开 Direct Post 三重 gate。
- prepare-only 窗口只记录当前 gate/schedule 状态，不修改它们。
- `direct_outro` 的 eligibility 不能自动开启 `TT_POST_LIVE_ENABLED`、`TT_POST_DIRECT_AUDIT_APPROVED`、`TT_POST_URL_PROPERTY_VERIFIED`。
- 本轮不改变既有 activation、账号可见性、评论或自动排期设置；变更窗口结束时按部署前状态恢复所有 runner/timer/path 单元。

## 数据库变更

### Additive schema

| 表 | 新增/确认字段 |
| --- | --- |
| `tt_post_material_intake` | `description TEXT NOT NULL DEFAULT ''`，以及 URL 所需 `material_tag` |
| `tt_post_recurring_pool` | `description TEXT NOT NULL DEFAULT ''`，以及 material/drama/language/tag 冻结字段 |
| `tt_post_queue` | `description TEXT NOT NULL DEFAULT ''`、material/drama/language/tag、`short_link_id`、`short_url`、`long_url` |

队列增加：

```sql
CREATE UNIQUE INDEX IF NOT EXISTS ux_tt_post_queue_short_link
ON tt_post_queue(short_link_id) WHERE short_link_id > 0;
```

### 迁移规则

1. 先用 SQLite online backup 备份数据库，并记录 SHA-256、size、row counts 和 `PRAGMA integrity_check`。
2. migration 只能 `ADD COLUMN`/`CREATE INDEX IF NOT EXISTS`，不 drop/rename/rebuild 生产表。
3. 已发布 queue 保持原 caption/description/short-link 数据，不回填当前源库。
4. available 老 pool 若模板含 `{desc}` 且 description 为空，迁移后保持不可发布；盘点后受控回填并重算，或重新入池。
5. migration 后执行 `PRAGMA integrity_check`、schema/index 检查和历史 row count 对比。

## 部署前门禁

- 最终 Git diff 已评审，确认 2c 与宏分支的六个交叠文件没有语义覆盖。
- `test-cases.md` 42/42 通过，P0/P1 为 0。
- 固定片尾和 Logo 的实际 SHA-256/size 已与已审核资产清单核对。旧资产清单记录的 `TT-new-outro.mp4` 基线为 4,202,613 bytes、SHA-256 `b6efd06c9304380aa118c4c3963057cc82e10ab569caa97d0cd9aeef588fe1fc`；部署时必须重新计算，任何不一致都要重新审核，不能盲信旧记录。
- 新 release、旧 CPU/GPU release、env、SQLite、manifest/publish ledger 和 Nginx 配置均已备份；备份路径和 hash 写入变更单。
- 记录 queue 数、publish ledger 数、TikTok 已知 Post 基线和所有 schedule `version/enabled/publish_times`。
- 发布 runner 在变更窗口不会消费新任务；不得通过保存生产 schedule 来达到隔离。

## 计划部署步骤

### A. 只部署代码/配置候选，不 activation

1. 从经评审 commit 构建 CPU release 与 GPU release，分别上传服务器只读 release 目录。
2. 校验 release commit、文件清单、owner/mode 和 Python 编译结果。
3. 备份 CPU SQLite/env/current symlink；备份 GPU env/secrets metadata、assets、manifests、publishes/current symlink；备份 Nginx active config。
4. 创建 `/mnt/data-disk/tt-post-publisher/s2l`，属主为 TT sidecar 服务账号，目录 `0755`；服务写入文件必须原子完成。
5. 将完整 TT location 加入 `gy.g2flow.com`，先执行 `nginx -t`，再 reload；用不存在 TT URL 验证 404 和安全 headers，并验证一条 X URL 不变。
6. 在数据库副本先跑 migration；核对无误后，在发布 runner 隔离的窗口切换 CPU release 并启动 migration。只执行只读健康检查。
7. 校验片尾/Logo hash 后切换 GPU release，设置 `direct_outro`、专用 COS 和 profile；先保持生产发布 gate 原状态，不触发 Post。
8. CPU `TT_POST_MEDIA_PROFILE_VERSION` 与 GPU `/health.profile` 完全一致后，才允许进入 prepare-only。

### B. prepare-only 验收

1. 保存二次 queue/ledger/schedule/Post 基线。
2. 使用全新唯一 job ID、不可变 source URL 和可用时的 source SHA/size，仅调用 GPU `/internal/tt-post/prepare`。不要创建生产 queue。
3. 核对 `/health`：`media_mode=direct_outro`、独立 profile、`direct_post_eligible=true`、`brand_overlay_review_required=false`、`transition=phone-match-0.9s`、COS health 正常。
4. 核对响应/manifest：mode/profile/source/outro/logo/trim/transition、output SHA/size/duration、manifest v4 和 storage backend。
5. 确认 output URL 不等于 source URL，host 为 `socialkit-cdn.yingliang.tech`；下载并校验 SHA/size、Range、Content-Type。
6. ffprobe、抽帧和人工观看确认源内容、旧尾裁剪策略、Logo/tutorial-outro、声音连续与 transition。
7. 再读 queue/ledger/schedule/Post 基线，必须完全无变化；HTTP audit 中 publish/canary/run-now/schedule-save 调用必须为 0。
8. 将真实证据回填 `test-report.md`。本轮到此结束，不执行正式 activation。

### C. 真实 Post 验收（本轮不执行）

只有新授权到达后，才可制定单独变更单：逐账号 creator-info、公开可见性、允许评论、用户 consent、三重 gate、自动排期和一条真实 Post 的回滚/监控。不允许把 prepare-only 的通过结果等同于正式发布通过。

## 验证步骤

### 离线/CPU

```powershell
python -m py_compile features/tt_posts/core.py features/tt_posts/service.py features/tt_posts/links.py features/tt_gpu/worker.py
python scripts/test_tt_post_links.py
python scripts/test_tt_posts_core.py
python scripts/test_tt_posts_service.py
python scripts/test_tt_post_pool_ui.py
python scripts/test_tt_post_prepare_runner.py
python scripts/test_tt_gpu_worker.py
python scripts/test_tt_posts_app_contract.py
```

### 服务器只读检查

```bash
systemctl status tt-post-service.service --no-pager
systemctl status tt-gpu-publisher.service --no-pager
curl --fail --silent http://127.0.0.1:8830/health
nginx -t
sqlite3 /mnt/data-disk/tt-post-publisher/tt-post.sqlite3 'PRAGMA integrity_check;'
```

对公网短链使用显式 no-cache 头，并分别请求 TT/X 形状 URL。不要将内部 token 输出到终端或日志；prepare 请求使用现有 root-only 安全 wrapper。

## 回滚方案

### CPU 宏/短链轨道

1. 保持发布 runner 不消费新任务，切回上一 CPU release 和上一份非 secret env。
2. additive columns/index 保留，不降库、不删除历史数据；旧代码应忽略新增列。
3. 隔离/阻断模板含 `{desc}`/`{url}` 且尚未发布的 pool/queue，避免旧 renderer 发布字面宏；记录 ID 供恢复后继续。
4. 若 Nginx location 有问题，先确认没有 active TT queue 依赖，再回退 Nginx config 并 `nginx -t`/reload。保留 wrapper 文件和数据库短链身份，不递归删除目录。
5. 验证旧无宏 caption、旧 X 短链、素材池查询和 schedule disable 仍正常。

### GPU `direct_outro` 轨道

1. 切回上一 GPU release。
2. 恢复上一 mode/profile 组合：正式 clean 基线为 `TT_POST_GPU_MEDIA_MODE=direct_clean` + `tt-post-direct-clean-hevc-720x1280-v1`，或按变更前记录恢复 branded preview；CPU expected profile 同步恢复。
3. 恢复相应 trim 值，检查 `/health` mode/profile/transition/eligibility。
4. 不删除 direct_outro manifest、COS object、asset 或 publish ledger；它们用于审计，且本轮没有真实发布需撤销。
5. 用全新隔离 job 做一条旧模式 prepare-only，确认旧合同恢复；仍不得调用 publish。

### 数据与 COS

- 若 migration 本身失败且数据库无法打开，停止服务并从已验证 online backup 恢复整个明确 SQLite 文件；恢复前再次核对目标绝对路径。
- 不在回滚中删除专用 COS object。对象可按后续获批的保留策略清理，本轮只记录 key/hash。
- 密钥轮换或泄露处理走腾讯云/密钥管理流程，不通过 Git 回滚。

## 注意事项

- 不把用户提供的 SecretID/SecretKey 写入仓库、文档、日志或聊天复述；现有明文凭据应由负责人按安全流程轮换。
- TT 专用 COS 仅用于 TT Post；其他业务使用原桶的配置不能随本部署改变。
- `direct_outro` 正式可直发不等于允许立即发；所有账号、consent、隐私/评论和全局 gate 继续生效。
- wrapper 长链基址固定为 `https://www.dramawavew2a.com/ads/101/2250/view`；短链基址为 `https://gy.g2flow.com/s2l`，不可混淆。
- 本文中的服务状态、asset hash 和路径必须在真正部署时重新读取；旧文档数据不能代替当前环境证据。
