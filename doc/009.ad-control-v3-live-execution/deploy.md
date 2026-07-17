# 部署文档

## 变更内容

V3 FB live pause/copy、手动 execute API、账号时区 runner、三张 ads_ai 表和动态 UI；不覆盖共享 monolith，不改 V2 cron。

## 配置项

```dotenv
AD_CONTROL_V3_LIVE_PAUSE_ENABLED=1
AD_CONTROL_V3_LIVE_COPY_ENABLED=1
AD_CONTROL_V3_COPY_PERSISTENCE_ENABLED=1
AD_CONTROL_V3_COPY_ACTIVATE_ENABLED=1
AD_CONTROL_V3_RUNNER_ENABLED=1
AD_CONTROL_V3_RUNNER_OBSERVE_RELEASED=1
AD_CONTROL_V3_RUNNER_LIVE_RELEASED=1
AD_CONTROL_V3_META_TIMEOUT_SECONDS=20
```

PAUSED Canary、created_data/lineage 校验和幂等恢复均已通过，上述为 2026-07-17 生产最终值。开关文件位于数据盘 `/mnt/data-disk/ai-ad-control-v3/config/runtime.env`。新规则默认禁用且只观察，开关开启不等于自动操作已有规则。

## 数据库变更

执行 `sql/003_enable_fb_live_pause_copy.sql`。DDL 前保存 `SHOW CREATE`、列/索引签名和 ads_ai 表清单；DDL 后验证 FB 镜像列/索引与来源完全相同。

## 部署步骤

1. 推送精确 Git commit。
2. 在数据盘创建包含 app、V3 runtime、runner、unit、env、cron、SQLite、DDL 基线的检查点。
3. 通过 `deploy/apply_ad_control_v3.py` 从当前线上精确 source commit 合并到 target commit。
4. 从同一 commit 安装 systemd service/timer；`daemon-reload`，先不启 timer。
5. 执行 DDL 并校验 schema。
6. 更新 env：先 pause/copy/persistence=1，activation/live runner=0。
7. 重启 API，执行 compile/API/UI/观察零写 smoke。
8. 执行一个 PAUSED copy Canary，核对 Meta/created_data/lineage。
9. 打开 activation/live runner，启用 timer；监控首个 tick。

## 本次生产发布记录

- 最终代码：`3a70e8346f5e77e47af3bb3cd943855386304460`。
- 生产 staging：`/mnt/data-disk/ai-ad-control-v3/staging/repo-6b4abd979389`，运行文件与目标 commit 精确比对结果为 `unchanged`。
- 发布前总检查点：`/mnt/data-disk/ai-ad-control-v3/backups/predeploy-live-pause-copy-20260717T025049Z-f5cfe61`，SHA256 清单校验通过，SQLite integrity 为 OK。
- 精确回滚包：
  - `/mnt/data-disk/ai-ad-control-v3/backups/ad-control-v3-ba5858ef661d-to-f5cfe61c382f`
  - `/mnt/data-disk/ai-ad-control-v3/backups/ad-control-v3-f5cfe61c382f-to-c4f5dcc77bfe`
  - `/mnt/data-disk/ai-ad-control-v3/backups/ad-control-v3-c4f5dcc77bfe-to-3a70e8346f5e`
- DDL 文件 SHA256：`6d6f8a458551932c0b1112d0e1d7be7e4e01d3575ba1504d9ae4e40530c825ae`。
- 实际审计数据：created_data 1 行、copy intent 1 行、lineage 1 行；因此不得通过删表回滚。

## 验证步骤

- `systemctl is-active drama-material-api.service`
- `systemctl status ad-control-v3-runner.timer`
- `/api/ad-control/v3/meta` 能力与 env 一致。
- preview 日志 `meta_write_count=0`。
- target 56 列和索引签名匹配。
- Canary 新对象 PAUSED、行数/素材/剧目/预算/出价一致。
- V2 cron 仍为原值，V2 规则可正常试算。

## 回滚方案

1. 60 秒熔断：将 `AD_CONTROL_V3_LIVE_COPY_ENABLED=0`、`AD_CONTROL_V3_RUNNER_LIVE_RELEASED=0`，必要时停用 timer；不影响 V2 pause。
2. 代码：使用 deployer 的精确 source/target 与数据盘 checkpoint 反向恢复。
3. unit/env/cron/SQLite：从本次 checkpoint 原子恢复并 daemon-reload。
4. DDL：本次已经产生真实审计行，禁止删表；只停止写入并保留审计。
5. Meta：根据 lineage 精确 PAUSE/隔离，不自动删除。

## 注意事项

API 是共享 monolith；禁止从仓库整份覆盖线上 `app.py`。任何 live hash 漂移必须停止部署并重新制作精确 overlay。
