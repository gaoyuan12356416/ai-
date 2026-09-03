# 开发计划

## 开发范围

在 `ops/tt-minis-bid-protection/` 实现 MySQL 5.7 DDL、Python 同步脚本和测试；在 CPU 服务器以 GitHub 精确提交部署，安装 root cron 并完成一次 60 天回填。

## 任务拆分

| 任务 | 负责人 | 文件/模块 | 状态 |
| --- | --- | --- | --- |
| 单表 DDL 与索引 | Codex | `001_create_ads_tiktok_minis_bid_protection_daily.sql` | 已完成 |
| 动态范围、API 分批、金额校验与 upsert | Codex | 同步脚本 | 已完成 |
| 幂等、失败隔离和脱敏测试 | Codex/QA | 测试模块 | 已完成（本地 21/21） |
| root cron、独立锁/日志与运维说明 | Codex | crontab、README | 已完成 |
| Token 安全轮换、建表、发布和 60 天回填 | Codex | CPU `43.166.187.96` | 待部署 |
| 生产读回与 `2026-09-02` 样本核对 | Codex/QA | `ads_ai`、脱敏日志 | 待部署 |

## 编译 / 构建命令

```bash
python3 -m py_compile ops/tt-minis-bid-protection/tt_minis_bid_protection_sync.py
python3 -m unittest discover -s ops/tt-minis-bid-protection -p 'test_*.py'
git diff --check
```

## 风险与依赖

- 依赖 TikTok 新 Token 同时拥有 Bid Protection 与既有 Native Growth 只读权限。
- 依赖 `ads_ai` 写入口 `63353` 和只读验收入口 `63350` 可用。
- API 历史窗口最大 60 天，回填需分账户、层级和日期切片，避免超出每请求 200 单元限制。
- 源码、服务配置与 DDL 必须从同一 GitHub 提交部署；秘密只保存在服务器现有受限位置。

## 完成记录

- 2026-09-03：需求/SA/QA 文档、单表 DDL、同步/轮换脚本与本地测试完成；生产部署与验收结果在完成后更新。
