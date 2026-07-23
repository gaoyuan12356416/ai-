# 2026-07-22 Dramawave X Canary 候选审计

## 结论

- 最终推荐素材：`5221348`。
- 审计时间：`2026-07-23 10:31:38 CST`。
- 查询入口：`101.32.56.53:63350`，从 CPU 主机 `43.166.187.96` 发起；实测 `@@read_only=1`，后端 MySQL 实例返回端口 `25437`。
- 全程仅执行只读 SQL、HTTP `HEAD`/Range 和本地 `ffprobe`；未刷新 X Token、未上传媒体、未创建短链、未发 Post，也未写生产数据库。
- 在 `2026-07-22`、`product='Dramawave'`、`resource_id` 非空且非 `0` 的素材消耗 Top 100 中，`5221348` 总消耗排名第 54，消耗 `1606.61`。它是按消耗顺序最靠前、同时满足保守的 X 普通 `tweet_video` 编码及竖屏推荐分辨率口径的素材。

## 候选字段

| 字段 | 值 | 来源/说明 |
| --- | --- | --- |
| material_id / resource_id | `5221348` | `ads_custom_source_insight.resource_id`、`ads_custom_source.id` |
| data_source_id | `3CRScaBEY0` | `ads_custom_source.data_source_id` |
| content_id | `3CRScaBEY0` | 与 `ads_drama_resource.content_id` 精确连接 |
| content_sign | 空字符串 | 不可用；本次以经验证的 `data_source_id -> content_id` 连接为准 |
| media URL | `https://advertising-1306474899.cos.ap-hongkong.myqcloud.com/kol-order/tasks/20260618/c8d57cadd00f70717bd4d8275f0c0919.mp4` | HTTPS、无签名参数 |
| material_name | `【推荐】2M_TROTLQ_(14_15超爽卡点)_EN_精剪_zhouliwei_3_episode[15].mp4` | `ads_custom_source.name` |
| language | `en` | `ads_custom_source.language=en`、`drama_language=en`；通用 insight `language` 有 `en,none`，不用于覆盖素材语言 |
| series_code | `21341` | 当日 insight 唯一值 |
| drama_name | `The Rise of the Lycan Queen` | `ads_drama_resource.name` |
| URL 业务标签 | `Fantasy` | 取自无风险的 `ads_drama_resource.labels`；不把素材级 `high_quality` 当剧标签 |
| desc | `A Silvermoon wolf princess, born powerless and cast away, returns 20 years later as the legendary Lycan Queen to reclaim her bloodline, avenge her mother, and rewrite the laws of the wolf realm.` | `ads_drama_resource.desc`，194 个字符 |

当日该素材共有 `132` 条 insight，`SUM(spend)=1606.61`；`resource_id`、`data_source_id`、`series_code` 和 `drama_language` 均分别只有一个有效值。

## Top 技术候选对比

Top 100 先按数据库元数据筛选 `type=2`、`is_delete=0`、`video_duration BETWEEN 1 AND 140`。在最终候选之前，仅有下列短视频候选；未列出的更高消耗素材为图片、数据库时长超过 140 秒或不满足视频元数据门禁。

| 总榜排名 | material_id | 消耗 | 实测时长 | 实测编码 | 分辨率 / FPS | 处理结果 |
| ---: | ---: | ---: | ---: | --- | --- | --- |
| 1 | `5656596` | `11988.23` | DB `191s` | 未继续下载 | — | 超过 140 秒，直接排除 |
| 11 | `5422726` | `5070.98` | `29.372993s` | HEVC Main + AAC-LC | `720x1280` / `30` | 普通 `tweet_video` 保守要求 H.264，排除 |
| 31 | `5698855` | `2436.52` | `29.350000s` | HEVC Main + AAC-LC | `720x1280` / `30` | HEVC，排除 |
| 44 | `5676777` | `1765.45` | `29.350000s` | HEVC Main + AAC-LC | `720x1280` / `30` | HEVC，排除 |
| 53 | `5751281` | `1607.42` | `130.579000s` | H.264 Main + AAC-LC | `1080x1920` / `30` | 接受门槛内，但不采用有文档歧义的 1080 竖屏 |
| **54** | **`5221348`** | **`1606.61`** | **`68.708005s`** | **H.264 Main + AAC-LC** | **`720x1280` / `30`** | **最终推荐** |

X 官方同一页面一处推荐竖屏 `720x1280`，另一处 Advanced 又写尺寸应在 `32x32` 到 `1280x1024` 之间；同时说明订阅用户可上传 1080p。为避免在账号订阅状态与竖屏尺寸解释不明确时冒险，本次采用其明确列出的竖屏推荐值 `720x1280`。参考：[X Media best practices](https://docs.x.com/x-api/media/quickstart/best-practices)。

## 违规与标签证据

### 历史违规/审核记录

| 数据源 | 匹配口径 | 命中数 |
| --- | --- | ---: |
| `ads_facebook_violations` | `source_id=5221348` | `0` |
| `ads_tiktok_violations` | `source_id='5221348' OR original_source_id='5221348'` | `0` |
| `ads_twitter_violations` | `source_id='5221348' OR original_source_id='5221348'` | `0` |
| `ads_resource_audit` | `resource_id=5221348` | `0` |

### 素材与剧标签

- `resource_tags` 对该素材只有一条赋值：`high_quality`。
- 素材标签对 `porn/nude/rape/bdsm/blood/gore/incest/sexual/violence/violent` 以及 `色情/暴力/情色/血腥/裸/强奸` 的命中数为 `0`。
- `ads_drama_resource.labels` 唯一值为：`Fantasy,Betrayal,Counterattack,Family Affection,Female Growth,Werewolf,Heiress,Boss Lady,Fantasy Realm`。
- 剧标签按同一危险词典检查，命中行数为 `0`。
- `ads_drama_resource` 的连接结果共有 `312` 行、覆盖 `4` 个 app_id，但 `name`、`labels`、`desc` 各只有 `1` 个 distinct 值，因此对本候选的剧名、标签和描述无歧义。
- 当前 `kunlunads_dev` schema 不存在名为 `task_tag_1` 或 `task_tag_2` 的列；`ads_drama_detail_insight` 也没有标签字段。当前能够核实的持久化标签来源是素材级 `resource_tags.tag_name` 与剧级 `ads_drama_resource.labels`。若后续产品规则硬性要求存在独立的 `task_tag_1/task_tag_2=clean` 记录，应在发布前 fail closed，而不能把“字段不存在”解释为已通过。

## 媒体可用性证据

从 CPU 主机对最终素材执行 HTTP 检查：

```text
HTTP/1.1 200 OK
Content-Type: video/mp4
Content-Length: 42312248
Accept-Ranges: bytes
Last-Modified: Thu, 18 Jun 2026 12:20:07 GMT

Range: bytes=0-1048575
HTTP 206
Downloaded bytes: 1048576
Content-Type: video/mp4
```

本地 `ffprobe 8.0.1` 直接读取同一公网 URL：

```text
container: mov,mp4,m4a,3gp,3g2,mj2
duration: 68.708005 seconds
size: 42312248 bytes

video:
  codec_name: h264
  profile: Main
  pixel_format: yuv420p
  width: 720
  height: 1280
  level: 50
  r_frame_rate: 30/1
  avg_frame_rate: 30/1

audio:
  codec_name: aac
  profile: LC
```

验证结果：时长在 `0.5–140s` 内、大小远低于 `512MB`、帧率不超过 `60fps`、比例为 `9:16`（在 `1:3–3:1` 内）、像素格式为 YUV 4:2:0、音频为 AAC-LC，并命中官方推荐的竖屏 `720x1280`。

## 脱敏 SQL

下列 SQL 不含用户名、密码、Token、内部 bearer 或签名参数。所有查询均通过 `63350` 只读入口执行。

### 只读与索引确认

```sql
SELECT @@hostname, @@port, @@read_only;

SELECT INDEX_NAME, SEQ_IN_INDEX, COLUMN_NAME, CARDINALITY
FROM information_schema.statistics
WHERE table_schema = 'kunlunads_dev'
  AND table_name = 'ads_custom_source_insight'
ORDER BY INDEX_NAME, SEQ_IN_INDEX;

EXPLAIN
SELECT s.resource_id, ROUND(SUM(s.spend), 2)
FROM kunlunads_dev.ads_custom_source_insight s FORCE INDEX (pss)
WHERE s.product = 'Dramawave'
  AND s.dt = '2026-07-22'
  AND s.resource_id NOT IN ('', '0')
GROUP BY s.resource_id
ORDER BY SUM(s.spend) DESC
LIMIT 100;
```

实测 `EXPLAIN.key=pss`；该索引顺序为 `product, dt, series_code`。

### Top 100 与素材元数据

```sql
SELECT
  t.resource_id,
  t.spend,
  t.series_code,
  t.insight_language,
  t.drama_language,
  cs.url,
  cs.name,
  cs.language,
  cs.data_source_id,
  cs.content_sign,
  cs.video_duration,
  cs.type,
  cs.is_delete,
  cs.tag_name
FROM (
  SELECT
    s.resource_id,
    ROUND(SUM(s.spend), 2) AS spend,
    SUBSTRING_INDEX(
      GROUP_CONCAT(DISTINCT NULLIF(s.series_code, '') ORDER BY s.series_code SEPARATOR ','),
      ',', 1
    ) AS series_code,
    SUBSTRING_INDEX(
      GROUP_CONCAT(DISTINCT NULLIF(s.language, '') ORDER BY s.language SEPARATOR ','),
      ',', 1
    ) AS insight_language,
    SUBSTRING_INDEX(
      GROUP_CONCAT(DISTINCT NULLIF(s.drama_language, '') ORDER BY s.drama_language SEPARATOR ','),
      ',', 1
    ) AS drama_language
  FROM kunlunads_dev.ads_custom_source_insight s FORCE INDEX (pss)
  WHERE s.product = 'Dramawave'
    AND s.dt = '2026-07-22'
    AND s.resource_id NOT IN ('', '0')
  GROUP BY s.resource_id
  ORDER BY SUM(s.spend) DESC
  LIMIT 100
) t
JOIN kunlunads_dev.ads_custom_source cs
  ON cs.id = CAST(t.resource_id AS UNSIGNED)
ORDER BY t.spend DESC, CAST(t.resource_id AS UNSIGNED);
```

数据库视频门禁是在上述 Top 100 外层增加：

```sql
WHERE cs.type = 2
  AND cs.is_delete = 0
  AND cs.video_duration BETWEEN 1 AND 140
```

### 最终候选回查

```sql
SELECT
  cs.id, cs.url, cs.name, cs.language,
  cs.data_source, cs.data_source_id, cs.content_sign,
  cs.video_duration, cs.type, cs.is_delete,
  cs.tag_name, cs.product
FROM kunlunads_dev.ads_custom_source cs
WHERE cs.id = 5221348;

SELECT
  ROUND(SUM(s.spend), 2) AS spend,
  COUNT(*) AS row_count,
  COUNT(DISTINCT s.series_code) AS series_count,
  MIN(s.series_code) AS series_code,
  COUNT(DISTINCT s.resource_id) AS resource_count,
  COUNT(DISTINCT s.data_source_id) AS data_source_count,
  GROUP_CONCAT(DISTINCT s.drama_language ORDER BY s.drama_language) AS drama_languages,
  GROUP_CONCAT(DISTINCT s.language ORDER BY s.language) AS insight_languages
FROM kunlunads_dev.ads_custom_source_insight s FORCE INDEX (pss)
WHERE s.product = 'Dramawave'
  AND s.dt = '2026-07-22'
  AND s.resource_id = '5221348';
```

### 违规记录

```sql
SELECT
  (SELECT COUNT(*)
   FROM kunlunads_dev.ads_facebook_violations f
   WHERE f.source_id = 5221348) AS facebook_count,
  (SELECT COUNT(*)
   FROM kunlunads_dev.ads_tiktok_violations t
   WHERE t.source_id = '5221348'
      OR t.original_source_id = '5221348') AS tiktok_count,
  (SELECT COUNT(*)
   FROM kunlunads_dev.ads_twitter_violations x
   WHERE x.source_id = '5221348'
      OR x.original_source_id = '5221348') AS twitter_count,
  (SELECT COUNT(*)
   FROM kunlunads_dev.ads_resource_audit a
   WHERE a.resource_id = 5221348) AS resource_audit_count;
```

### 素材标签与危险词

```sql
SELECT rt.id, rt.source_id, rt.product, rt.tag_name
FROM kunlunads_dev.resource_tags rt
WHERE rt.source_id = 5221348
ORDER BY rt.id;

SELECT COUNT(*) AS dangerous_tag_count,
       GROUP_CONCAT(rt.tag_name ORDER BY rt.id SEPARATOR ',') AS dangerous_tags
FROM kunlunads_dev.resource_tags rt
WHERE rt.source_id = 5221348
  AND (
    LOWER(rt.tag_name) REGEXP
      'porn|nude|rape|bdsm|blood|gore|incest|sexual|violence|violent'
    OR LOCATE(UNHEX('E889B2E68385'), CAST(rt.tag_name AS BINARY)) > 0 -- 色情
    OR LOCATE(UNHEX('E69AB4E58A9B'), CAST(rt.tag_name AS BINARY)) > 0 -- 暴力
    OR LOCATE(UNHEX('E68385E889B2'), CAST(rt.tag_name AS BINARY)) > 0 -- 情色
    OR LOCATE(UNHEX('E8A180E885A5'), CAST(rt.tag_name AS BINARY)) > 0 -- 血腥
    OR LOCATE(UNHEX('E8A3B8'), CAST(rt.tag_name AS BINARY)) > 0       -- 裸
    OR LOCATE(UNHEX('E5BCBAE5A5B8'), CAST(rt.tag_name AS BINARY)) > 0 -- 强奸
  );
```

### 剧信息连接与标签门禁

```sql
SELECT
  COUNT(*) AS row_count,
  COUNT(DISTINCT r.app_id) AS app_id_count,
  COUNT(DISTINCT r.name) AS name_count,
  COUNT(DISTINCT r.labels) AS labels_count,
  COUNT(DISTINCT COALESCE(r.desc, '')) AS desc_count,
  MIN(r.name) AS drama_name,
  MIN(r.labels) AS labels,
  CHAR_LENGTH(MIN(COALESCE(r.desc, ''))) AS desc_chars,
  MIN(COALESCE(r.desc, '')) AS drama_desc,
  MIN(r.content_id) AS content_id,
  MIN(r.series_code) AS series_code,
  MIN(r.language) AS language
FROM kunlunads_dev.ads_drama_resource r FORCE INDEX (content_id)
WHERE r.content_id = '3CRScaBEY0'
  AND r.series_code = '21341'
  AND r.language = 'en';

SELECT COUNT(*) AS dangerous_rows,
       GROUP_CONCAT(DISTINCT r.labels ORDER BY r.labels SEPARATOR '\t') AS dangerous_labels
FROM kunlunads_dev.ads_drama_resource r FORCE INDEX (content_id)
WHERE r.content_id = '3CRScaBEY0'
  AND r.series_code = '21341'
  AND r.language = 'en'
  AND (
    LOWER(r.labels) REGEXP
      'porn|nude|rape|bdsm|blood|gore|incest|sexual|violence|violent'
    OR LOCATE(UNHEX('E889B2E68385'), CAST(r.labels AS BINARY)) > 0
    OR LOCATE(UNHEX('E69AB4E58A9B'), CAST(r.labels AS BINARY)) > 0
    OR LOCATE(UNHEX('E68385E889B2'), CAST(r.labels AS BINARY)) > 0
    OR LOCATE(UNHEX('E8A180E885A5'), CAST(r.labels AS BINARY)) > 0
  );
```

## 发布前边界

- 本文只证明 `5221348` 在当前可查询生产数据和视频技术检查下为首个保守可发候选，不代表 X 已实际接受媒体。
- 真正发布仍必须在 sidecar 账号锁内重新检查账号动态状态、Token 刷新结果、`tweet.write/media.write` scope，并以 X `FINALIZE/STATUS` 的实时处理结果为准。
- 任何标签来源缺失、媒体处理失败或账号状态异常均应终止本次 canary，不自动切换账号或素材连续尝试。
