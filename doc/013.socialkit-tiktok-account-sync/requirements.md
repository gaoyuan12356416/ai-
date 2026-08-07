# 013.socialkit-tiktok-account-sync 需求与技术设计

## 背景

SocialKit 已通过外部 CynosDB 暴露 TikTok 个号授权信息与最新指标。AI 后台所在 CPU 服务器可以访问该源库，但 `ads_ai` 尚无可供内部系统稳定读取的个号快照表。业务需要将帖子数、粉丝数、播放量、点赞、评论、收藏、分享、明文 access token 与 Token 状态按小时落到 `ads_ai`。

## 目标

- 新增 `ads_ai.tiktok_personal_account_snapshot` 当前快照表。
- 每 5 分钟同步 `platform=3 AND is_deleted=0` 的 SocialKit TikTok 个号，固定在分钟数 `02/07/12/.../57` 触发，缩短 Token 自动续期后的目标快照滞后。
- 以 `social_center_accounts.id` 为主键，关联 `social_account_data.team_id + account_id` 获取指标。
- 明文 Token 只存在数据库字段和进程内存，不进入代码、Git、命令行、日志或任务输出。
- 保持幂等、单事务、单实例、失败不清空现有数据。

## 范围

### 包含

- 源表 `socialkit.social_center_accounts` 与 `socialkit.social_account_data` 的只读查询。
- 目标表一次性 DDL、参数化 upsert、消失账号停用与 Token 清空。
- 独立 systemd oneshot 与 timer，每 5 分钟执行一次，并保留单实例锁避免重叠。
- 源/目标专用 root-only 环境文件、固定端点校验、运行日志脱敏。

### 不包含

- 不同步 Facebook/YouTube。
- 不同步 `refresh_token`，避免扩大敏感数据面。
- 不回写 SocialKit，不调用 TikTok 发布接口，不验证 Token 真实 API 可用性。
- 不删除目标历史行；源账号删除/消失后仅置 `is_active=0` 并清空 `access_token`。
- 不修改 AI 后台 HTTP API 或页面。

## 用户故事 / 业务规则

1. 内部系统能按 TikTok 个号读取最新指标与当前 access token 状态。
2. 仅同步未逻辑删除的 TikTok 个号；已删除源账号的 Token 不得继续保留在目标表。
3. `token_status=2` 只是源状态；使用 Token 时仍需同时检查过期时间、账号状态和发布禁用状态。
4. 源账号没有指标行时仍保留账号与 Token，七项指标置 0，`has_metric_snapshot=0`。
5. 源查询返回 0 行、重复主键或超过 1000 行时 fail closed，不更新目标。
6. 目标固定为 `101.32.56.53:63353/ads_ai`，运行时代码禁止 DDL，失败零重试，等待下一小时。

## 交互与流程

```text
systemd timer（每 5 分钟，:02/:07/.../:57）
  -> 获取主机文件锁
  -> 只读查询 SocialKit 现有 TikTok 个号 + 左连接指标
  -> 校验非空、主键唯一、行数上限
  -> 连接固定 ads_ai 写端点并确认 @@read_only=0
  -> 单事务逐行参数化 upsert
  -> 将本批未出现的旧目标行置 inactive 并清空 access_token
  -> commit
  -> journal 仅输出计数与 run_id
```

## 技术设计

### 影响模块

- `scripts/sync_socialkit_tiktok_accounts.py`：源查询、校验、目标事务同步和脱敏汇总。
- `deploy/socialkit-tiktok-account-sync.service|timer`：小时调度与系统级安全限制。
- `deploy/socialkit-tiktok-account-sync.env.example`：无密钥配置模板。
- `001_create_tiktok_personal_account_snapshot.sql`：一次性目标表 DDL。

### 数据结构

目标表以 `source_account_id` 为主键，保存团队/平台账号标识、七项指标、明文 `access_token`、Token/账号状态、纳秒过期与巡检时间、指标是否存在、源更新时间、本批同步标识和目标更新时间。`is_active=0` 的行必须为 `access_token IS NULL`。

### API / 接口

本需求不新增 HTTP API。运维入口仅为：

```bash
python3 scripts/sync_socialkit_tiktok_accounts.py --dry-run
python3 scripts/sync_socialkit_tiktok_accounts.py
systemctl start socialkit-tiktok-account-sync.service
```

### 异常与边界

- 源库不可用、权限不足或行数为 0：退出非 0，目标保持不变。
- 目标表不存在、端点错误、`@@read_only!=0` 或写入失败：事务回滚，零重试。
- 同步重叠：主机文件锁使第二个进程安全跳过。
- 源账号存在但指标缺失：指标置 0，不丢失账号/Token。
- 源账号从当前集合消失：仅在全部 upsert 成功后停用并清空 Token。
- 数据库状态不代表 TikTok API 实际有效；调用方需另做窄范围 canary。

## 验收标准

- 首次同步后目标活动行数等于源库未删除 TikTok 个号数。
- 每个活动目标行的七项指标与源表关联结果一致；缺指标账号明确标记。
- Token 非空数量、正常/过期状态计数与源库一致，但日志与测试输出不出现 Token 原文。
- 相同源数据连续同步两次不增加行数，第二次仍成功。
- 模拟源账号消失后目标行 `is_active=0 AND access_token IS NULL`。
- 错误端点、错误目标库、空源、重复主键和超量源均 fail closed。
- systemd timer enabled/active，下一次触发时间符合 `:02/5` 五分钟周期。

## 风险与待确认

- 明文 Token 在 `ads_ai` 中扩大了可访问面；必须限制数据库授权，并避免在通用报表/UI 中暴露该列。
- 当前 `ads_aius` 对 `ads_ai` 有 broad privileges；本需求通过固定端点/库/表和 root-only 配置降低风险，但不能替代未来 DBA 建立最小权限账号。
- 源表当前 24 个活动 TikTok 账号中有 1 个缺少指标快照；该情况按业务规则保留账号并写 0 指标。
- 当前快照不保留指标历史。若后续需要趋势，另建历史事实表，不能直接扩张本任务。

## 变更记录

| 日期 | 内容 |
| --- | --- |
| 2026-07-22 | 初版：TikTok 个号指标与明文 access token 小时级同步。 |
