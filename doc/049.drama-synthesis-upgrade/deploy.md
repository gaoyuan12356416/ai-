# 部署与回滚（GitHub-first；本候选禁止执行）

## 门禁

- QA commit 通过；备份 CPU SHA/systemd/nginx/env metadata/SQLite，不读取 secret；outputs dry-run 后事务迁移。
- 外部 owner 提供三张统一表；否则 sync 保持失败/待重试，不能宣称链路通过。
- 建专用 owner/root `/mnt/data-disk/drama-youtube-short-links/s2l/youtube` 和 dedicated nginx location；数字 namespace 不共用。
- `YOUTUBE_LIVE_ENABLED=0`；源 allowlist 精确为 `advertising-1306474899.cos.ap-hongkong.myqcloud.com,ai.yingliangads.com`，无 wildcard。

## HK dark release

1. 从已 QA GitHub SHA 创建 `/data/drama-synthesis-gpu/releases/<git_sha>` 并安装锁定依赖。
2. 复制 FB v3 资产到独立 immutable root，核对 manifest SHA `028326ab211418934b026c227f2e3707553cce7560551dca3c0bfddc681d566f`、20 files、520297533 bytes 和每文件 SHA。
3. 安装 `drama-synthesis-gpu-worker.service`，WorkingDirectory `/data/drama-synthesis-gpu/current`，监听 `127.0.0.1:8787`；不改 `ads_video_producer.service`。
4. 安装独立 tunnel，复用受控 key/known_hosts，把 CPU `127.0.0.1:18788` 映射到 HK 8787；确认 legacy 18787。
5. health/catalog 后用固定样本验 concat/no-bgm/cover/random、codec/audio/COS/recipe；队列 idle/drain 后显式切 CPU URL。无 fallback/dual-write。

## CPU 与回滚

- GitHub SHA fast-forward；`nginx -t`；migration 先 `--dry-run`，再 `--apply --backup <new-absolute-path>`，二次 `--dry-run` zero changes；重启 API/job/YouTube worker并验证六 API/schema/outbox/logs。
- 真实 canary 仅在用户另行指定当前产品测试频道并精确授权后执行：unlisted、宏、评论、统一表读回。当前没有授权。正式按钮还需合规门禁。
- 回滚先关闭 live；不得把 processing/unknown 给旧 worker。CPU 回上一 SHA，GPU 切 legacy 18787 后停新 tunnel，HK current 指回上一 release。
- additive schema、短链、external IDs、outbox 保留；不反向 DDL、不删外部对象。任何 YouTube 删除需新授权。
