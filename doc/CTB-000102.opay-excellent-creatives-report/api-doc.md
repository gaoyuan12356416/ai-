# API 与数据契约

## 2026-08-27 图片/视频全基准与1000美元门槛（最新确认）

路径、schema_version=2及34列CSV保持不变；`selection_policy.google.version=picvid_cpc_1000_v2`。GG benchmark.spend/clicks/impressions/ctr/cpc和audit.platform_spend/platform_cpc/metric_source统一改为全部图片/视频池，metric_source=`ads_google_insights:type=3,asset_type=2/4`；platform_fx_*指图片视频池FX缺口。新增evidence.platform_spend_scope/platform_cpc_scope=`google_picture_video_assets`、rule_b_min_spend_usd=1000；policy新增baseline_source/cpc_weighting/min_spend。baseline_missing_account_days保留为Campaign参考缺日，不阻断新口径。其他渠道字段原样冻结。

## 共同约束

- 基础路径：`/reports/opay-excellent-creatives/`。
- 公开只读，无 Cookie/Token 要求。
- HTML 和 `latest.json` 使用 `Cache-Control: no-store`；版本化 JSON/缩略图可长期缓存。
- 所有响应带 `X-Robots-Tag: noindex, nofollow`；HTML 同时包含 robots meta。
- 金额单位 USD，时间使用 ISO 8601 北京时区或 `yyyy-mm-dd`。

## 接口列表

### `GET /reports/opay-excellent-creatives/`

返回静态 HTML。无结尾 `/` 的路径 301 到规范路径，不跳转飞书。

### `GET /reports/opay-excellent-creatives/latest.json`

公开提交清单：

```json
{
  "schema_version": 1,
  "data_version": "20260826T120000000000+0800",
  "generated_at": "2026-08-26T12:00:00+08:00",
  "latest_month": "2026-07",
  "months": [
    {
      "month": "2026-07",
      "stage": "final",
      "generated_at": "2026-08-05T10:00:00+08:00",
      "row_count": 26,
      "status": "success"
    }
  ]
}
```

`latest.json` 只在该版本全部月份文件和 HTML 准备完成后原子替换。

### `GET /reports/opay-excellent-creatives/data/<version>/<yyyy-mm>.json`

顶层字段：

- `schema_version`、`month`、`stage`、`generated_at`、`keyword_config_version`。
- `rows`：入选素材；无优秀素材不含占位行。
- `benchmarks`：六个渠道/App 基准。
- `audits`：六个渠道/App 计算状态、映射覆盖和排除原因。
- `stage_diff`：相对该月初版/上一可见版的行数和素材 ID 差异。

素材行核心字段：

```json
{
  "month": "2026-07",
  "channel": "Meta",
  "app": "NG OPay",
  "custom_source_id": 4566082,
  "material_type": "VID",
  "thumbnail_url": "assets/thumbnails/4566082-....jpg",
  "source_url": "https://...mp4",
  "source_status": "available",
  "spend": 8000.0,
  "impressions": 4000000,
  "clicks": 20000,
  "installs": 1490,
  "af_d0_first_transactions": 234,
  "maker": "胡杰",
  "first_launch_date": "2026-05-13",
  "first_launch_source": "platform_success",
  "selling_points": [],
  "selling_point_status": "pending",
  "selection_rule": "A+B",
  "evidence": {}
}
```

`evidence` 至少包含素材/平台 CTR、CPA、消耗排名、累计占比、是否在前 50%、A/B 判定、映射覆盖和数据质量说明。无穷 CPA 以 JSON `null` 表示并由 `*_cpa_finite=false` 解释，禁止输出非标准 `Infinity`。

### `GET /reports/opay-excellent-creatives/assets/thumbnails/<file>`

返回同源缓存 JPEG/PNG。文件名由素材 ID 和内容哈希组成，不接受目录穿越。

## 错误码

- 静态文件不存在：404。
- Nginx/发布异常：5xx；正常生成失败不切换清单，因此用户继续看到上一成功版本。
- CLI 以非零退出码表示配置、读取、计算、冻结或发布失败，并输出不含凭据的 JSON 错误摘要。

## 兼容性说明

- `schema_version=1` 内只允许新增字段；删除/改义需升版本。
- 前端忽略未知字段，缺失缩略图/源文件字段时显示降级状态。
- 旧 AI Game Performance 路径和鉴权契约不受影响。

## V2 API / 数据契约追加（2026-08-27）

上文schema1示例与V1兼容说明保留为历史证据；本节对齐2026-08-27已稳定的schema2实现/接口。实现方反馈51项后端测试、34项前端行为契约通过；此反馈不替代本文件作者的独立执行、生产canary或最终QA验收，也不代表接口已上线。

### V2 版本与访问约定

- 四个静态路径、公开只读、robots、缓存头和错误返回保持不变，不新增浏览器直连 MySQL/API。
- `latest.json.schema_version` 与全部候选月数据均为 `2`；仍使用 `data_version`，没有 `version` 替代字段。第一批公开 V2 清单必须包含完整 `2026-01`—`2026-07` 七个月，不混入 schema 1 月文件或 8 月。
- `data/<data_version>/<yyyy-mm>.json` 不可变；先准备全部版本文件/媒体/兼容 HTML，最后原子替换 `latest.json`。客户端在一次会话中按读取到的同一 `data_version` 切月，不能混拼版本。
- `rows`仍是“月份×渠道×App×custom_source_id”入选素材，Google公开名称不改成GG；ID为JSON正整数，不是字符串/浮点/布尔值。Meta/TikTok的rows/benchmarks/audits全部原字段原样保留，仅rows/benchmarks新增同形六项metrics，不能拿平台指标填素材null。

### 六项公式、类型与单位

设 `S=spend`（USD）、`I=impressions`、`C=clicks`、`N=installs`、`A=af_d0_first_transactions`，均先按同月同素材汇总。任一必需输入为 null/不可用时，该项为 null；conversion 不是 N 或 A。

| metrics 键 | 名称 | 公式 | JSON单位 / 精度上限 | 已知零边界 |
| --- | --- | --- | --- | --- |
| `d0_cpa` | D0首交CPA | `S/A` | USD/首交，6位小数 | A=0时null；S=0且A>0为0 |
| `cpm` | CPM | `S/I*1000` | USD/千曝光，6位小数 | I=0时null；S=0且I>0为0 |
| `apm` | APM | `A/I*1000` | AF D0首交/千曝光，8位小数 | I=0时null；A=0且I>0为0 |
| `ctr` | CTR | `C/I` | 比率，8位小数 | 输入均已知且I=0时沿用V1为0；C=0且I>0为0 |
| `cvr` | CVR | `N/C` | 点击→安装比率，8位小数 | C=0时null；N=0且C>0为0 |
| `install_to_d0_rate` | 安装→D0首交转化率 | `A/N` | 比率，8位小数 | N=0时null；A=0且N>0为0 |

- 所有row/benchmark均有六个键，值仅有限JSON number或null，不省略null键/输出NaN/Infinity/数字字符串。APM不是百分比，页面表格/详情固定4位，CSV保留JSON原精度（最多8位）；CTR/CVR/安装转化率只在页面乘100，CSV存原始比率，不封顶。
- D0 首交真实为0时，JSON `d0_cpa=null`、对应已知零证据标记成本非有限；页面/CSV可沿用“∞”文字。AF未知时也是JSON null，但必须由 `af_status`/原始null区分，页面/CSV留空，绝不能显示∞或0。
- FX/映射缺口不能用`0`、平台总量、Google conversions或旧证据字段回填 `row.metrics`。schema 2 中显式 null 是权威值，不得回退为其他计算结果。

### GG 素材行与详情字段

| 字段 | V2类型/意义 | GG约束 |
| --- | --- | --- |
| `spend` | number，规范化USD | 入选素材必须月金额完整；cost先除1,000,000、逐事实历史换汇后月聚合，再按分单位归一 |
| `impressions` / `clicks` | 非负integer | 来自严格映射的type3资产事实，不是type0/广告组分摊 |
| `installs` | integer或null | 素材级缺失，固定null；不能用conversions替代 |
| `af_d0_first_transactions` | integer或null | 素材级缺失，固定null；不能分摊平台AF |
| `platform_conversions` | 有限非负number，允许小数 | GG“Google平台转化数”仅详情，保留源精度、不截整数；不在主表/主CSV/metrics/选优中使用 |
| `selection_rule` | `A` / `B` / `A+B` | GG只可能B；Meta/TT规则不变 |
| `metrics` | 上述六项对象 | GG只有CPM/CTR可计算，其他四项null |
| `evidence.mapping_status` | 映射质量 | GG入选必须exact；全部source候选链合法且一致到同一真实OPay素材，不能过滤非法候选后只接受好链 |
| `evidence.metric_source` | 指标来源 | GG为`ads_google_insights:type=3` |
| `evidence.af_status` / `installs_status` | 可用性原因 | GG为`missing_asset_attribution` / `missing_asset_installs`，页面显示中文原因 |
| `evidence.usd_status` / `fx_sources` | USD核验摘要 | 入选金额verified；详细币种、raw micros、日期/账户、历史候选及对账证据保留在隔离缓存/审计 |
| `evidence.rule_a_available` / `rule_a_pass` | A开关/结果 | GG均false，不能被coverage或平台AF打开 |

以下为结构/公式示例，不是生产数据；素材6000 USD、CTR 2%，平台CTR 1%但平台金额未知，仍可命中B：

```json
{
  "month": "2026-07",
  "channel": "Google",
  "app": "NG OPay",
  "custom_source_id": 123456,
  "material_type": "VID",
  "spend": 6000,
  "impressions": 1000000,
  "clicks": 20000,
  "installs": null,
  "af_d0_first_transactions": null,
  "platform_conversions": 37.125,
  "selection_rule": "B",
  "metrics": {
    "d0_cpa": null,
    "cpm": 6,
    "apm": null,
    "ctr": 0.02,
    "cvr": null,
    "install_to_d0_rate": null
  },
  "evidence": {
    "mapping_status": "exact",
    "metric_source": "ads_google_insights:type=3",
    "af_status": "missing_asset_attribution",
    "installs_status": "missing_asset_installs",
    "usd_status": "verified",
    "fx_sources": ["historical_reconciled"],
    "material_ctr": 0.02,
    "material_cpa": null,
    "material_cpa_finite": null,
    "platform_ctr": 0.01,
    "platform_cpa": null,
    "platform_cpa_finite": null,
    "platform_cpa_available": false,
    "exact_mapping_spend_coverage": null,
    "rule_a_available": false,
    "rule_a_pass": false,
    "rule_b_pass": true
  }
}
```

### GG 平台基准、审计与 nullable

- `benchmarks[].spend/impressions/clicks/ctr` 的 GG平台事实来自同月同App的 `ads_google_insights.type=0`，不取入选素材之和。完整曝光/点击的 CTR 与 FX 是否可用无关。
- 完整性另有硬门禁：每个GG asset-day都必须存在同App、同账户、同日期的Campaign `type=0` account-day；缺少任一项时，该月该App的GG规则B整体暂停，平台impressions/clicks/CTR为null，审计`baseline_missing_account_days`记录缺口并显示中文原因。其他月份/App和Meta/TT不受影响；不得借别的账户/日期基准或把缺行当零。
- 基准 `spend` 与 `cpa`、审计 `platform_spend`、`mapping_coverage`、`mapping_gap_spend`、素材证据中的平台CPA/覆盖率允许 null。平台有任何未知金额时，不把已知部分当完整平台spend，CPA不可用；CTR完整时仍保留并供B比较。
- GG平台安装无可信源时为null；已有可核验的平台聚合AF D0可以保留为平台证据，不能成为素材AF/规则A。CPA仅在平台USD及AF均可用时按同口径计算，零AF与缺失USD通过`cpa_finite`/`platform_cpa_available`区分。
- `*_cpa_finite=true`表示有限值，false表示已知零首交导致非有限，null表示输入未知；不得仅凭null CPA显示∞。金额未知时 `platform_cpa_available=false`，必须留空。
- `exact_mapped_spend`是已核验的精确USD子集，不是未知金额估算。`fx_missing_rows`、`platform_fx_missing_rows`、`incomplete_material_count`及`mapping_status_counts`披露FX/映射缺口；`baseline_missing_account_days`单独披露基准缺行。须区分“仅FX缺失但CTR完整”与“Campaign account-day缺失导致CTR不完整/B暂停”。
- `fx_missing_native_spend`为素材type3缺FX原币，新增`platform_fx_missing_native_spend`为Campaign type0缺FX原币；均为`{币种: 有限非负number}`对象，未知币种用`UNKNOWN`、无缺口时`{}`，原币金额最多2位小数。禁止跨币种求和/标USD或回填平台spend。
- 历史FX以同账户/日`exchange_rate`、`last_exchange_rate`候选对正消耗`spend/spend_usd`核验：空/非法候选跳过，另一候选仍可成立；正消耗历史行缺USD不跳过/当0。无唯一可核验汇率则金额fail-closed，不影响完整Campaign CTR。
- GG `af_mapped`、`af_mapping_coverage` 为null；审计中平台聚合AF不表示素材已归因。已知平台零消耗的coverage沿用0；未知平台消耗的coverage必须null。
- 业务缺口有明确中文审计原因；数据读取/计算/发布异常为失败，旧清单不变。未刷新不能冒充“计算成功且全0”。

### 页面、CSV 与历史兼容

- 主表/详情统一读六项metrics；APM固定4位（0.05851234显示0.0585），CSV仍为0.05851234。GG `platform_conversions`仅详情、允许有限非负小数并保留源精度，注明“不等于安装/AF D0”，不套APM四位格式。前端不重算schema2指标或用`value || 0`抹平null。
- CSV使用当前筛选及排序，六项顺序为D0首交CPA、CPM、APM、CTR、CVR、安装→D0首交转化率；USD/千曝光/原始比率单位以上表为准，缺失为空字段，真实0保留，已知零首交CPA的∞仅为显示文本例外。保留UTF-8/引号/换行转义，不导出conversions列。
- V2前端需兼容schema1旧快照；历史缺字段时只可使用同义且输入已知的旧证据，不能把不存在的数据补0。schema1原JSON不改写，不能仅改版本号冒充schema2。

### CLI、默认缓存与冻结兼容契约

- V2默认缓存为`<OPAY_REPORT_DATA_ROOT>/cache/opay-excellent-creatives-v2.sqlite3`，生产默认完整路径`/mnt/data-disk/opay-excellent-creatives/cache/opay-excellent-creatives-v2.sqlite3`。V1旧名`cache/opay-excellent-creatives.sqlite3`保留不变，供只读clone与原release回滚使用。
- 路径优先级：显式`--cache-db` > `OPAY_REPORT_CACHE_DB` > 对应release的默认文件名。实现方说明现有生产env未设`OPAY_REPORT_CACHE_DB`，新代码自动选择V2默认，无需改env/Nginx/timer；发布前仍需现场复核，不能盲目安装示例env或覆盖既有设置。
- 数据根仍为`/mnt/data-disk/opay-excellent-creatives`；cache分文件，快照按`snapshots/<month>/<stage>/<version>.json`新增版本，影子输出固定为其`staging-public-v2`。旧快照/内容散列缩略图原地保留，供clone中的绝对路径/preserved rows读取；不新建独立数据根或复制全部历史媒体，不允许覆写V1。

| 参数/组合 | 契约 |
| --- | --- |
| `--clone-cache-from PATH --cache-db NEW_PATH` | SQLite只读一致性备份到新目标，包含已提交WAL；源必须存在、源目标不同、目标不得已存在，失败不刷新/发布 |
| `--google-only --refresh` | 只刷新独立GG表；要求已有冻结基线，并先比较Meta/TT事实/选优签名，再preserve原行/基准/审计，仅追加metrics；缺基线、哈希不符或签名变化即失败 |
| `--google-only` 不带 `--refresh` | 参数错误；仅配`--publish`或`--check-cache`也不允许 |
| 已冻结月不带 `--rebuild` | 跳过/拒绝改写；clone或google-only都不能绕过冻结 |
| 已冻结月带 `--rebuild` | 仅在已核验V2副本重算，保留旧快照与差异；不表示授权改V1源缓存 |
| `--publish` | 发布到显式`--output-dir`，先影子后正式；只发布时不附`--google-only`，不自动刷新或再次clone |
| 回归脚本 `--non-google-only` | 用真实V2月payload比较冻结V1的Meta/TT签名，过滤GG预期新增，不放过Meta/TT变化；默认fixture是2026-07，不能替代其他六个月的逐月对账 |

Google-only成功重建时，月数据`upgrade_audit`包含`baseline_schema_version`、`baseline_generated_at`、`preserved_non_google_rows`和`non_google_facts_and_selection_unchanged=true`。这是可核验的升级证据，不代替实际payload回归。V2不改变原有初版/终版常规刷新入口；未来新月份不能用仅历史修正的google-only代替全量刷新。具体命令、发布门禁和默认缓存回滚见`deploy.md`的V2章节。

## Google CPC / 图片视频 CTR 契约增量（2026-08-27）

保持schema_version=2、data_version及现有接口；以下Google专属增量覆盖上文历史B-only/Campaign-B口径。Meta/TT所有原字段/metrics保持，不允许通过忽略字段掩盖变化。

| 路径 | 新增/变更含义 |
| --- | --- |
| `selection_policy.google` | 对象含`version: "cpc_picvid_v1"`、`rule_a`、`rule_b`、`operator: "OR"`及跨线/并列、加权口径说明；所有公开可见月必须同此policy版本 |
| `rows[].selection_rule`（Google） | A、B或A+B；A=全Campaign月USD累计50%（含跨线/并列）且严格低CPC；B=素材USD>5000且严格高PIC+VID加权CTR |
| `rows[].evidence.material_cpc / platform_cpc` | 素材/全Campaign USD消耗除以点击，有限number或null；单位USD/点击。不是AF CPA，零点击不可比 |
| `rows[].evidence.rule_a_metric` | Google固定`"cpc"`；Meta/TT仍原AF CPA，不借此改写其证据 |
| `rows[].evidence.rule_a_unavailable_reason` | A可用为空；不可用为中文原因：平台月USD不完整、平台金额非正/点击0、完整精确映射候选不足平台50% |
| `rows[].evidence.platform_ctr` | Google改为同月同App全部PIC+VID资产总点击/总曝光；非Campaign CTR、非日/素材比率均值 |
| `rows[].evidence.platform_ctr_scope` | Google固定`"google_picture_video_assets"`；全部type3 asset_type2/4，包含未映射资产 |
| `benchmarks[]`（Google） | 原`spend/impressions/clicks/ctr`及metrics仍为Campaign；追加`cpc`、`picture_video_ctr`、`picture_video_clicks`、`picture_video_impressions` |
| `audits[]`（Google） | 原审计字段保留，追加`rule_a_metric`、`rule_a_unavailable_reason`、`eligible_mapped_spend`、`platform_cpc`、`picture_video_ctr/clicks/impressions`及`rule_b_ctr_source`；平台CPC对应benchmark.cpc |

前端原表25列和六项metrics不变。CSV原31列名称/顺序/数值不改，末尾新增第32列`素材CPC USD/点击`、第33列`平台CPC USD/点击`、第34列`平台CTR口径`。CPC使用证据原始数值，scope导出原字符串；非Google新列为空，历史未提供则为空；原`平台CTR`列仍在原位置，但新Google值为图片视频基准并由末列明示。转换数仍不进CSV/六项metrics，null仍留空、真实0不抹去。

Google CPC页面显示6位小数并注明USD/点击，只影响展示不参与规则；CSV不沿用舍入。规则说明按`selection_policy.google.version`识别新政策，并兼容证据`rule_a_metric=cpc`；历史未升级快照不误标新规则。

映射增加`source_type=6 → ads_youtube_videos → 原ads_source(source_type=3) → ads_custom_source.id`精确桥接，原direct-type3不变；桥接ID、App、视频类型及全部候选合法一致均须验证，禁止拿YouTube ID当素材ID。默认缓存更新为`<DATA_ROOT>/cache/opay-excellent-creatives-google-cpc.sqlite3`，CLI/env显式路径优先级不变；旧V2缓存只读保留。新验收入口为`validate_google_cpc_upgrade.py --baseline-dir <V2public> --candidate-dir <stage> --cache-db <new> --baseline-cache <old>`，须验证所有候选及入选/基准/证据、全MetaTT字段和旧表哈希，不只复核入选行。
