# 部署文档

## 变更内容

V3 FB live pause/copy、手动 execute API、账号时区 runner、三张 ads_ai 表和动态 UI；不覆盖共享 monolith，不改 V2 cron。

## 配置项

```dotenv
AD_CONTROL_V3_LIVE_PAUSE_ENABLED=1
AD_CONTROL_V3_LIVE_COPY_ENABLED=1
AD_CONTROL_V3_COPY_PERSISTENCE_ENABLED=1
AD_CONTROL_V3_COPY_ACTIVATE_ENABLED=0
AD_CONTROL_V3_RUNNER_ENABLED=1
AD_CONTROL_V3_RUNNER_OBSERVE_RELEASED=1
AD_CONTROL_V3_RUNNER_LIVE_RELEASED=0
AD_CONTROL_V3_META_TIMEOUT_SECONDS=20
```

先以 PAUSED Canary 发布；确认 created_data/lineage 后才能将两个 live/activation release 开关打开。开关文件位于数据盘 `/mnt/data-disk/ai-ad-control-v3/config/runtime.env`。

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

## 验证步骤

- `systemctl is-active drama-material-api.service`
- `systemctl status ad-control-v3-runner.timer`
- `/api/ad-control/v3/meta` 能力与 env 一致。
- preview 日志 `meta_write_count=0`。
- target 56 列和索引签名匹配。
- Canary 新对象 PAUSED、行数/素材/剧目/预算/出价一致。
- V2 cron 仍为原值，V2 规则可正常试算。

## 回滚方案

1. 60 秒熔断：将 `AD_CONTROL_V3_LIVE_COPY_ENABLED=0`、`RUNNER_LIVE_RELEASED=0`，停用 timer；不影响 V2 pause。
2. 代码：使用 deployer 的精确 source/target 与数据盘 checkpoint 反向恢复。
3. unit/env/cron/SQLite：从本次 checkpoint 原子恢复并 daemon-reload。
4. DDL：无真实行时可删新表；有真实行后不删表，只停止写入并保留审计。
5. Meta：根据 lineage 精确 PAUSE/隔离，不自动删除。

## 注意事项

API 是共享 monolith；禁止从仓库整份覆盖线上 `app.py`。任何 live hash 漂移必须停止部署并重新制作精确 overlay。
