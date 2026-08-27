# 部署与回滚（2026-08-27 现行；GitHub-first）

## 当前结论与授权

最新已 QA、GitHub push/readback 的代码候选为 `c719bebf72be900ec3853858dc53b36b83beffd2`。独立合并回归 166/166 PASS，不代表正式发布放行。HK 已运行该 dark release，auto 真实媒体/COS 下载、时长、帧数与解码通过，但同 job/payload 第二次 POST 幂等断言失败；缓存窄修仅本地完成，独立回归、HK 增量发布与真实复验待完成。CPU 主应用未切流。实时证据以 [测试报告](test-report.md) 和 [HK 实测记录](hk-gpu-setup-20260827.md) 为准。

用户已授权环境完成后继续部署，并指定 Shahrul Ikmal 做一次内部 unlisted 视频及一条评论。所有支持/服务器操作仅通过 SSH；禁止腾讯云管理后台、public 测试、无关平台发布及修改现有 X/ads_video_producer。不新建收费云集群。旧文档“尚未授权 canary”“必须 Tencent API 云备份”的口径已失效。

生产 ads_aius 对 kunlunads_dev 只有 SELECT/SHOW VIEW；尚无合法 admin/migrator/writer。该权限阻塞未解除，禁止生产 DDL、启用未经健康验证的 writer 或真实 YouTube 上传/评论。三表隔离恢复演练不提供生产账号权限。

## 不可跳过的门禁

- 保存 CPU/HK 原 Git SHA、服务 PID、systemd/Nginx/env 元数据、SQLite 一致性备份与端口；密钥、数据库行、真实配置不进入 Git/报告。
- 新代码从 GitHub 精确 SHA 拉取并保留回滚版本；离线测试通过不能替代运行验收。
- CPU 正式制作路径保持 legacy 127.0.0.1:18787；HK dark 仅通过新 18788 隧道访问 8787。完整媒体、幂等、队列 idle/drain 与其他发布前置条件均通过后才能显式切换，无 fallback/dual-write。
- 当前路径是 [migration.md](migration.md) 的真实三表 READ ONLY 一致性 snapshot + CPU 本机隔离 MySQL 5.7 恢复演练。机器生成的 SHA 绑定证据已 PASS，仅覆盖三表，不代表全集群备份/灾备已验证。
- 生产 DDL 使用一次性 drama_youtube_migrator@43.166.187.96，仅三张精确表的 SELECT/INSERT/CREATE/ALTER；长期 drama_youtube_writer@43.166.187.96 仅 SELECT/INSERT/UPDATE，从未持有 DDL。禁止 schema wildcard、DELETE/DROP/INDEX/GRANT OPTION 与额外 routine/proxy 权限；迁移后撤销并销毁 migrator。
- 正式 live/sync 均保持 0。精确 source allowlist 为 advertising-1306474899.cos.ap-hongkong.myqcloud.com,ai.yingliangads.com，禁止 wildcard。

## 路径、服务及凭据隔离

| 用途 | 固定合同 |
| --- | --- |
| HK 媒体代码 | `/data/drama-synthesis-gpu/releases/<git_sha>`；current 原子指向已验收版本 |
| HK 解释器/模型/资产 | /data/drama-synthesis-gpu/runtime/current/bin/python；独立 models/assets/work/results；无 /root 依赖、无 YouTube/DB 发布凭据 |
| HK worker | 专用 drama-synthesis-gpu 用户；127.0.0.1:8787；保留 ProtectHome/ProtectSystem 与实际 ReadWritePaths；render 并发默认 1 |
| CPU 新制作隧道 | 127.0.0.1:18788→HK 8787；保留原 18787、18820 与 X 隧道限制 |
| 统一 writer | 127.0.0.1:18837；18836 属于 FB reverse tunnel，禁止占用 |
| writer 代码 | root:root 0755 的 `/opt/drama-youtube-unified-writer/releases/<candidate_git_sha>`，current 指向候选 |
| gy wrapper | /mnt/data-disk/drama-youtube-short-links/s2l/youtube；复用现有域名/TLS/Nginx，不修改 X `/s2l/<数字>.html` |

`deploy/drama-youtube-unified-writer.env.example` 冻结服务配置。RPC 同值凭据分别保存在 root:root 0600 的客户端文件和 drama-youtube:drama-youtube 0600 的服务端文件；writer DB JSON 也是后者所有。文件必须为 regular、非 symlink、当前执行身份所有；仅比较 SHA，不打印内容，不通过命令行传 token/password。

`deploy/configure_drama_youtube_short_link_root.sh --check` 必须验证三层目录 drama-youtube:drama-youtube 0750、Nginx r-x access ACL、新文件 r-- default ACL 及 0640 文件合同。不得靠 chmod 放宽凭据或沙箱目录权限通过验收。

## 分阶段部署步骤

1. HK 精确发布依赖与媒体资产：固定 Python 3.10.20、torch/torchaudio 2.5.1+cu124、完整依赖锁、Demucs 本地四模型；核对 FB v3 manifest 028326ab211418934b026c227f2e3707553cce7560551dca3c0bfddc681d566f、20 文件/520297533 bytes 及逐文件 SHA。
2. 在与实际 worker 一致的 systemd 用户、资源限额及 ReadWritePaths 下预检，验证 health/auth/catalog、concat/no-BGM/cover-intro/random、完整双模式、重复 POST 幂等和失败后恢复。仅前一条成功或普通服务用户命令成功不足以切流。当前重复 POST 失败需先关闭缺陷。
3. 按迁移文档核对真实三表 snapshot/恢复证据。当前已 PASS 的数据为视频 244151、评论 53、日志 55105；候选、manifest、report、evidence SHA 必须逐项匹配，不能手填 PASS、沿用过期证据或伪装成云 API 备份。
4. 由合法管理员提供最小权限 migrator/writer 后，才执行生产 dry-run→带 evidence/candidate SHA 的 apply→二次 dry-run→撤销 migrator→最终 writer 验证。当前在此权限门禁 HOLD，不能拿 ads_aius 替代。
5. 检查 writer 路径可遍历、代码可读，并以实际服务身份执行：

```sh
runuser -u drama-youtube -- /usr/bin/python3 \
  /opt/drama-youtube-unified-writer/current/scripts/migrate_drama_youtube_unified_schema.py \
  --credential-file /etc/drama-youtube/writer-db.json \
  --cluster-id cynosdbmysql-5kxxsre7 \
  --verify-runtime-writer
```

6. 独立启动 writer，保留鉴权的 GET /health。必须返回 contract=drama-youtube-writer-preflight-v1、固定 schema/writer_identity、writable/schema_verified/indexes_verified=true 和 grant_fingerprint。仅 URL/凭据存在不是健康通过；401、跳转、超时、只读实例或结构/权限漂移均阻断，不允许先刷新 OAuth/上传再发现 writer 不可用。
7. CPU SQLite 先只读 dry-run，再带新绝对备份路径事务迁移；完成前后 integrity/输出合同检查。Nginx -t、短链 root --check；X 现有路径仍 200，YouTube 未生成数字路径 404、POST 403。
8. 全部门禁通过后才按已授权范围发布 CPU 主应用、drain 后显式切向 18788，并执行以下单次 canary。正式 public 入口不得因测试而打开。

## 指定频道内部 canary

身份固定为 Shahrul Ikmal：app 1479、channel_local_id 263、youtube_account_id 255、channel_id UCHJ1jFaYuW8g5EM7hM5pPpg；唯一 operation 为 drama-hk-deploy-unlisted-20260827-shahrul-263。浏览器不能创建/claim 此任务，普通 worker 和普通 outbox 跳过 canary。

CLI 私有配置来自继承环境或绝对路径、当前用户所有的 0600 JSON 文件；配置中 YOUTUBE_LIVE_ENABLED=0、DRAMA_YOUTUBE_UNIFIED_SYNC_ENABLED=0，显式指定真实 CPU job DB、短链 owner/root、视频 allowlist、work root 和受控 RPC。下面路径为配置示例；job 与 operator 必须取已核验真实值，不能照抄占位符或另给 source URL。

```sh
python scripts/drama_youtube_canary.py --action prepare \
  --config-file /etc/drama-youtube/canary-env.json \
  --authorize-unlisted-canary \
  --operation-id drama-hk-deploy-unlisted-20260827-shahrul-263 \
  --confirm-app-id 1479 --confirm-channel-local-id 263 \
  --confirm-channel-id UCHJ1jFaYuW8g5EM7hM5pPpg --confirm-account-id 255 \
  --operator-user-id "<已核验操作员ID>" \
  --job-id "<已完成生产任务的32位job_id>" --source-kind concat_video
```

prepare 从任务真实结果取 source，生成描述 {{url}} 短链并冻结唯一 unlisted 行，不调用 YouTube。记录返回的精确 task_id，再运行：

```sh
python scripts/drama_youtube_canary.py --action run \
  --config-file /etc/drama-youtube/canary-env.json \
  --authorize-unlisted-canary \
  --operation-id drama-hk-deploy-unlisted-20260827-shahrul-263 \
  --confirm-app-id 1479 --confirm-channel-local-id 263 \
  --confirm-channel-id UCHJ1jFaYuW8g5EM7hM5pPpg --confirm-account-id 255 \
  --operator-user-id "<已核验操作员ID>" --canary-task-id "<prepare返回的task_id>"
```

同一个 run 仅推进该任务一次并处理最多三类本任务 outbox；等待处理后复用同 task_id/operation 对账，不新建替代任务。先只读 RPC 预检，再按需 OAuth refresh/mine=true 验证目标身份；上传前落 session intent。视频必须读回 processed/succeeded/unlisted 后才能评论；评论必须确认真实 snippet.topLevelComment.id；每条同步前还要 fresh status。隐私漂移、会话身份丢失或评论 unknown 均 hold，不自动更新为 public、不盲重试。

验收必须是安全输出 complete=true，且视频 unlisted、恰好一条评论、三类统一记录各一条和幂等复跑都满足。submitted、processing、单有 video_id 或 ok=true/complete=false 均非最终成功。`--action status --canary-task-id <id>` 只读既有 ledger；原始凭据、session URI 和异常正文禁止输出。

## 回滚与历史边界

- 当前 CPU 尚未切流时，只回退/停止新增 HK release/tunnel，不切动原 18787，不停止旧 X 或 ads 服务。SSH key 回退必须比对原备份与当前文件 SHA，发现并发变化即停止，不覆盖其他 key。
- 若后续已切流：先关闭 live/sync、停止新 claim并审查 processing/unknown，drain 后按记录切回 legacy 18787，再停新 tunnel；CPU/HK current 分别回退已记录 GitHub SHA。
- 停统一 writer、回应用 SHA，保留 additive 列/索引、短链、external IDs 和 outbox；禁止反向 DDL、删外部对象、用共享库整库恢复回退无关写入。不得把未知发布任务交给不兼容的旧 worker。
- 本次表级 snapshot/rehearsal 是限定迁移的数据保护与可恢复性证据，不证明全集群灾备。YouTube 删除、全集群恢复或新增付费云资源均不在此次测试授权内。
- 2026-08-26 的 Wave8/增量候选与备份 API 方案仅为历史；其测试数、授权状态与云路径均不作为当前操作依据。
