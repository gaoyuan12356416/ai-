# 开发计划

## 开发范围

在 `ops/tt-minis-bid-protection/` 实现 MySQL 5.7 DDL、账户范围 Python 同步脚本和测试；在 CPU 服务器以 GitHub 精确提交部署，安装每天两次的 root cron 并完成一次 30 天重建回填。

## 任务拆分

| 任务 | 负责人 | 文件/模块 | 状态 |
| --- | --- | --- | --- |
| 单表 DDL 与索引 | Codex | `001_create_ads_tiktok_minis_bid_protection_daily.sql` | 已完成 |
| 精确账户范围、Campaign API 分批、金额校验与 upsert | Codex | 同步脚本 | 已完成 |
| 幂等、失败隔离和脱敏测试 | Codex/QA | 测试模块 | 已完成（本地 27/27） |
| root cron、独立锁/日志与运维说明 | Codex | crontab、README | 配置已完成，待安装 |
| Token 安全轮换、建表和精确 release 发布 | Codex | CPU `43.166.187.96` | 已完成 |
| 旧事实备份/清空、最近 30 天回填与幂等复跑 | Codex | CPU `43.166.187.96`、`ads_ai` | 待执行 |
| 三产品账户/Campaign 单层覆盖与 `2026-09-02` 样本核对 | Codex/QA | `ads_ai`、脱敏日志 | 待执行 |

## 编译 / 构建命令

```bash
python3 -m py_compile ops/tt-minis-bid-protection/tt_minis_bid_protection_sync.py
python3 -m unittest discover -s ops/tt-minis-bid-protection -p 'test_*.py'
git diff --check
```

## 风险与依赖

- 依赖 TikTok 新 Token 同时拥有 Bid Protection 与既有 Native Growth 只读权限。
- 依赖 `ads_ai` 写入口 `63353` 和只读验收入口 `63350` 可用。
- TikTok 历史接口强制 `query_ids`，无法直接仅传账户；脚本只能在精确账户范围内按日期取得 Campaign ID 并按单账户切片。
- 源码、服务配置与 DDL 必须从同一 GitHub 提交部署；秘密只保存在服务器现有受限位置。

## 完成记录

- 2026-09-03：需求/SA/QA 文档、单表 DDL、同步/轮换脚本与本地测试完成；生产部署与验收结果在完成后更新。
- 2026-09-03：release `8668e31373e592b34538fc911d88fa14caa2fa28` 已部署；生产 DDL 读回和 572 账户 Token 写前/写后兼容 canary 通过。首次 60 天回填、幂等、落表覆盖、cron 和样本查询待完成。
- 2026-09-03：用户将范围修订为 916 个账户的精确 SQL、Campaign 单层、30 天重建和每天两次 14 天刷新；兼容层代码与 27 项本地测试已完成，待发布和数据重建。
