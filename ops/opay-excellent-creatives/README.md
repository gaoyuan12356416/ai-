# OPay 月度优秀素材公开报表

公开地址：`https://ai.yingliangads.com/reports/opay-excellent-creatives/`。

## 边界

- 只统计素材产品 `OPay/Opay`；数据 App `OPay`、`OPay NGN`、`OPayPakistan`。
- Meta/TikTok 只保留 `ads_source`、`resource_id`、`ads_custom_source.id` 三方一致的素材事实。
- Google 使用 `ads_google_insights type=3, asset_type=2/4` 的图片/视频素材事实；A消耗/CPC和B的CTR基准均取全部图片/视频资产（含未映射），不混入Campaign/文字，不做广告组或AF分摊。
- Google `cost / 1000000` 还原原币，非USD使用可对账的历史汇率。素材任一天汇率不全则整月不评优，平台美元总额不全则留空。
- Google 当前代码政策为`picvid_cpc_1000_v2`：A=完整素材消耗累计至同月同App全部图片/视频USD的50%（含跨线/并列）且素材CPC严格低于图片/视频总USD消耗÷总点击；A没有最低消耗限制。B=素材USD>1000且CTR严格高于全部PIC+VID总点击÷总曝光，A OR B，标记A/B/A+B。平台USD未知/非正、点击0或完整映射素材消耗不足平台池50%时A暂停，B独立判断。用户已确认PK小额A素材也入选。
- Google 安装和素材级AF留空，`conversions`仅详情；source6视频允许经`ads_youtube_videos → 原ads_source(type3) → custom_source`严格桥接，全部候选须合法一致。Meta/TikTok的A仍为AF D0首交CPA，规则与事实不变。
- 六项 `metrics` 为 `d0_cpa/cpm/apm/ctr/cvr/install_to_d0_rate`；JSON版本2区分null与真实0。
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

## 当前 Google CPC 隔离升级（2026-08-27，生产结果待补）

新默认缓存为`cache/opay-excellent-creatives-google-picvid-1000.sqlite3`，从现行`google-cpc.sqlite3`一致性克隆，旧缓存/快照/媒体保留；显式CLI/env路径优先级不变。Google-only历史重建仍需`--refresh --rebuild`。详情显示CPC/基准来源/A暂停原因，CSV原31列不变、末尾追加2个CPC及CTR口径列。

```text
python validate_google_cpc_upgrade.py --baseline-dir <当前public> --candidate-dir <新stage> --cache-db <新google-picvid-1000.sqlite3> --baseline-cache <旧google-cpc-cache>
python validate_frontend_contract.py --payload <每个真实候选月.json>
```

独立验收覆盖原缓存全部候选/入选/基准/证据、Meta/TT全字段（含metrics）及旧缓存表哈希。GitHub-first精确SHA，所有可见月policy必须为`picvid_cpc_1000_v2`，不可变数据先准备、`latest.json`最后原子替换；不改其他report/timer。实际状态见`doc/CTB-000102.opay-excellent-creatives-report/release-google-picvid-1000-20260827.md`。

## 历史：V1 → V2 隔离升级（旧V2专用，非当前执行步骤）

历史V2仅允许Google规则B，并以Campaign CTR为B基准；当时默认缓存为`cache/opay-excellent-creatives-v2.sqlite3`，V1缓存禁止原地升级。以下旧命令仅保留历史，当前升级使用上节新默认cache和验收器，不直接执行本节。

```bash
python3 opay_excellent_creatives.py --clone-cache-from /mnt/data-disk/opay-excellent-creatives/cache/opay-excellent-creatives.sqlite3 --check-cache
python3 opay_excellent_creatives.py --backfill --from-month 2026-01 --to-month 2026-07 --stage final --refresh --google-only --rebuild
python3 opay_excellent_creatives.py --backfill --publish --output-dir /mnt/data-disk/opay-excellent-creatives/staging-public-v2
python3 validate_regression_snapshot.py /path/to/v2/2026-07.json --non-google-only
```

Google-only 重建强制对照每月原终版 Meta/TikTok 的基础指标和入选集合；媒体、关键词、制作者及原审计也从冻结快照保留。正式切换前必须完成全部月份、独立数据校验和页面测试。代码回滚时旧版重新使用原V1缓存，不覆盖其事实或快照。

真实产物验证（不连接数据库）：

```bash
python3 validate_v2_upgrade.py --baseline-dir /usr/share/nginx/html/reports/opay-excellent-creatives --candidate-dir /mnt/data-disk/opay-excellent-creatives/staging-public-v2
python3 validate_frontend_contract.py --payload /path/to/v2/2026-07.json --csv-output-dir /mnt/data-disk/opay-excellent-creatives/qa/csv
```

前者独立比较七个月全部非Google字段并重新计算六项公式；后者用真实月JSON执行页面原始CSV导出函数，对全部结果和Google筛选结果的Blob逐字段解析，生成可复核CSV。自动Blob检查不等同于确认某台浏览器的原生下载保存位置。
