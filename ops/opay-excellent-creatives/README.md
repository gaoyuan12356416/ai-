# OPay 月度优秀素材公开报表

公开地址：`https://ai.yingliangads.com/reports/opay-excellent-creatives/`。

## 边界

- 只统计素材产品 `OPay/Opay`；数据 App `OPay`、`OPay NGN`、`OPayPakistan`。
- Meta/TikTok 只保留 `ads_source`、`resource_id`、`ads_custom_source.id` 三方一致的素材事实。
- Google V1 不做广告组或 AF 分摊；平台基准与精确缺口仍展示。
- AF 固定 `data_source=0` + `revenue_event_count1_0`，且运行时验证产品配置事件 `First_Transaction`。
- 浏览器只读取静态 JSON；刷新仅访问只读端口 63350。

## 本地验证

```powershell
python -m py_compile ops\opay-excellent-creatives\opay_excellent_creatives.py
python -m unittest discover -s ops\opay-excellent-creatives -p "test_*.py" -v
python ops\opay-excellent-creatives\validate_frontend_contract.py
```

2026-07 固化回归：

```bash
python3 validate_regression_snapshot.py /path/to/data/<version>/2026-07.json
```

关键词配置由用户工作簿只读生成：

```powershell
node ops\opay-excellent-creatives\import_keywords.mjs `
  C:\Users\gaoyu\Downloads\OPay素材卖点关键词.xlsx `
  ops\opay-excellent-creatives\selling_points.v2026-08-26.json
```

## 生产 CLI

最近完整月份初版：

```bash
python3 opay_excellent_creatives.py --latest-complete-month --stage initial --refresh --publish
```

最近完整月份终版：

```bash
python3 opay_excellent_creatives.py --latest-complete-month --stage final --refresh --publish
```

首次历史回填：

```bash
python3 opay_excellent_creatives.py --backfill --from-month 2026-01 --stage final --refresh --publish
```

终版默认冻结。只有经过审计的历史修正才使用 `--rebuild`。`latest.json` 是公开提交点，任何失败都不得替换上一成功版本。
