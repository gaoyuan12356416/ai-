# 部署与回滚（GitHub-first；应用候选尚未执行）

## 门禁

- QA commit 通过；备份 CPU SHA/systemd/nginx/env metadata/SQLite，不读取 secret；outputs dry-run 后事务迁移。
- 三张统一表已确认存在于 `kunlunads_dev`。DDL 使用独立一次性 `drama_youtube_migrator@43.166.187.96`，逐表仅 SELECT/INSERT/CREATE/ALTER；长期 `drama_youtube_writer@43.166.187.96` 逐表仅 SELECT/INSERT/UPDATE，永不获得 DDL。两者都禁止 schema wildcard、DELETE/DROP/INDEX/GRANT OPTION；迁移后销毁 migrator。脚本会通过 `SHOW GRANTS` 与 information_schema 精确读回，额外权限直接阻断。
- 备份门禁不是一段手填文字：`--apply` 必须读取 root-owned 0600 evidence JSON，内容来自 Tencent CynosDB API 的 48 小时内 SUCCESS 备份读回；evidence 必须绑定非生产恢复实例 ID、最终 candidate Git SHA、脚本内置 migration contract SHA-256 和脱敏演练结果文件 SHA-256。常规回滚不恢复共享生产集群。
- loopback writer 固定 `127.0.0.1:18837`；18836 属于现有 FB random-overlay reverse tunnel，禁止占用或修改。`deploy/drama-youtube-unified-writer.env.example` 冻结 service env。同一随机 32+ 字符 token 写成两份同值文件：`unified-rpc-client.token` 为 root:root 0600，`unified-rpc-server.token` 为 `drama-youtube:drama-youtube` 0600；writer DB JSON 也为 `drama-youtube:drama-youtube` 0600。全部必须是 regular、非 symlink；两份 token 只比较 SHA-256 确认同值，不打印内容；调用端 URL/client-token path 必须成对配置。
- 复用现有 X 渠道的 `gy.g2flow.com` DNS、TLS 证书和 Nginx server，不新建域名/server block、不修改 X `/s2l/<数字>.html`；dedicated `/s2l/youtube/` location 位于 X 通用数字 location 之前。运行 `deploy/configure_drama_youtube_short_link_root.sh --check` 校验三层目录 `drama-youtube:drama-youtube` 0750、Nginx r-x access ACL 与新文件 r-- default ACL；任何漂移阻断应用启动/切换。
- `YOUTUBE_LIVE_ENABLED=0`；源 allowlist 精确为 `advertising-1306474899.cos.ap-hongkong.myqcloud.com,ai.yingliangads.com`，无 wildcard。

## HK dark release

1. 从已 QA GitHub SHA 创建 `/data/drama-synthesis-gpu/releases/<git_sha>` 并安装锁定依赖。
2. 复制 FB v3 资产到独立 immutable root，核对 manifest SHA `028326ab211418934b026c227f2e3707553cce7560551dca3c0bfddc681d566f`、20 files、520297533 bytes 和每文件 SHA。
3. 安装 `drama-synthesis-gpu-worker.service`，WorkingDirectory `/data/drama-synthesis-gpu/current`，监听 `127.0.0.1:8787`；不改 `ads_video_producer.service`。
4. 安装独立 tunnel，复用受控 key/known_hosts，把 CPU `127.0.0.1:18788` 映射到 HK 8787；确认 legacy 18787。
5. health/catalog 后用固定样本验 concat/no-bgm/cover/random、codec/audio/COS/recipe；队列 idle/drain 后显式切 CPU URL。无 fallback/dual-write。

## CPU 与回滚

- GitHub SHA fast-forward 后确认 `ss` 中 18837 空闲且 18836 原监听不变；安装 writer unit/env，执行 `systemd-analyze verify`。先保持所有 live/sync 开关为 0。
- `nginx -t`；执行短链 root `--check`；既有 X `/s2l/<数字>.html` 必须仍为 200，YouTube 不存在数字路径为 404，POST 为 403。已完成的基础配置不生成任何短链文件。
- CPU SQLite migration 先 `--dry-run`，再 `--apply --backup <new-absolute-path>`，二次 dry-run zero changes。
- 统一 MySQL 按 `migration.md` 顺序执行：backup API 读回与隔离恢复演练、一次性 migrator dry-run/apply（必须同时传 `--candidate-git-sha`）/二次 dry-run、销毁 migrator、长期 writer 三表最小授权。CPU 候选安装到 root:root 0755 的 `/opt/drama-youtube-unified-writer/releases/<candidate_git_sha>`，`current` 指向它；先用 `namei -l` 和 `runuser -u drama-youtube -- test -r` 验证路径可遍历且代码可读，再原样执行：

```sh
runuser -u drama-youtube -- /usr/bin/python3 \
  /opt/drama-youtube-unified-writer/current/scripts/migrate_drama_youtube_unified_schema.py \
  --credential-file /etc/drama-youtube/writer-db.json \
  --cluster-id cynosdbmysql-5kxxsre7 \
  --verify-runtime-writer
```

凭据文件和 grant 内容禁止进入 shell history、Git、日志或报告；root 直接运行因 0600 精确 owner 被拒绝属于正常门禁，不得 chmod 放宽。
- 启动 loopback writer 后，用 Bearer health 验证 `ok/schema/grant_fingerprint`；再启动 API/job/YouTube worker，验证六 API/schema/outbox/logs。health 返回 401、只读实例、端口漂移、schema/grant/index mismatch 均阻断 release。
- 真实 canary 仅在用户另行指定当前产品测试频道并精确授权后执行：unlisted、宏、评论、统一表读回。当前没有授权。正式按钮还需合规门禁。
- 回滚先关闭 live；不得把 processing/unknown 给旧 worker。CPU 回上一 SHA，GPU 切 legacy 18787 后停新 tunnel，HK current 指回上一 release。
- 常规回滚关闭 unified sync/live，停 loopback writer、回上一 GitHub SHA；additive schema、短链、external IDs、outbox 保留，不反向 DDL、不恢复共享 CynosDB、不删外部对象。任何 YouTube 删除或灾难恢复需新授权。
