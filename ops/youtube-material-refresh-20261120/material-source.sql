SELECT
  s.id AS id,
  COALESCE(s.name, '') COLLATE utf8mb4_general_ci AS name,
  CASE WHEN s.url LIKE 'http://advertising-1306474899.cos.ap-hongkong.myqcloud.com/%'
    THEN CONCAT('https://', SUBSTRING(s.url, 8)) ELSE COALESCE(s.url, '') END AS url,
  CASE WHEN s.cover LIKE 'http://advertising-1306474899.cos.ap-hongkong.myqcloud.com/%'
    THEN CONCAT('https://', SUBSTRING(s.cover, 8)) ELSE COALESCE(s.cover, '') END AS thumbnail_url,
  COALESCE(s.data_source_id, '') AS content_id,
  '' AS source_job_id,
  '' AS source_kind,
  COALESCE(s.name, '') AS macro_name,
  '' AS macro_desc,
  COALESCE(s.language, '') AS language,
  CASE WHEN s.video_duration > 0 THEN CONCAT(FLOOR(s.video_duration / 60), ':', LPAD(MOD(s.video_duration, 60), 2, '0')) ELSE '' END AS duration,
  '' AS size,
  CASE WHEN LOCATE('dramawave',LOWER(TRIM(s.product)))=1 OR LOCATE('[w2a]dramawave',LOWER(TRIM(s.product)))=1 OR LOWER(TRIM(s.product)) IN ('drama-b','drama-c','drama-comics','drama-novel','drama-ai素材','drama-社媒专用素材','[w2a]drama-double') THEN 1479 ELSE COALESCE(app.id,1479) END AS app_id,
  uploaders.user_id AS uploader_id,
  COALESCE(NULLIF(TRIM(u.name), ''), NULLIF(TRIM(u.username), ''),
    CASE WHEN COALESCE(s.user_id, 0)=0 THEN '未知上传人' ELSE CONCAT('用户 ', s.user_id) END) AS uploader_name
FROM `kunlunads_dev`.`ads_custom_source` s
LEFT JOIN (SELECT DISTINCT user_id FROM `kunlunads_dev`.`ads_custom_source`) uploaders
  ON uploaders.user_id=s.user_id
LEFT JOIN `kunlunads_dev`.`admin_users` u ON u.id=s.user_id
LEFT JOIN `kunlunads_dev`.`ads_apps_setting` app
  ON app.name=TRIM(s.product) COLLATE utf8mb4_unicode_ci
WHERE s.type=2
