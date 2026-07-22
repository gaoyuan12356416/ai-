# API 文档

## 接口列表

本需求不新增 HTTP API。唯一命令行接口：

```bash
python3 scripts/sync_socialkit_tiktok_accounts.py --dry-run
python3 scripts/sync_socialkit_tiktok_accounts.py
```

## 请求/响应

所有数据库参数通过 `/etc/socialkit-tiktok-account-sync.env` 注入。成功只输出一行脱敏 JSON，例如：

```json
{"status":"ok","source_rows":24,"metric_rows":23,"missing_metric_rows":1,"access_tokens_present":24,"target_upsert_operations":24,"target_deactivated_rows":0,"run_id":"<32-hex>"}
```

禁止在响应中加入账号名、主页、external ID、access token、密码或 SQL 参数。

## 错误码

| 退出码 | 含义 |
| --- | --- |
| 0 | 同步成功、dry-run 成功，或已有实例运行而安全跳过 |
| 1 | 配置、源查询、数据校验或目标事务失败；目标事务回滚 |

## 兼容性说明

- Python 3.9+。
- 依赖服务器现有 `PyMySQL`。
- 固定 SocialKit MySQL 8/CynosDB 源和 `ads_ai:63353` 写端点。
- Token 时间字段保持源库纳秒时间戳，不转换精度。
