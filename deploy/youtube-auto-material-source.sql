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
  '1479' AS app_id,
  COALESCE(CAST(s.user_id AS CHAR), '0') AS uploader_id,
  COALESCE(NULLIF(TRIM(u.name), ''), NULLIF(TRIM(u.username), ''),
    CASE WHEN COALESCE(s.user_id, 0)=0 THEN '未知上传人' ELSE CONCAT('用户 ', s.user_id) END) AS uploader_name
FROM `kunlunads_dev`.`ads_custom_source` s
LEFT JOIN `kunlunads_dev`.`admin_users` u ON u.id=s.user_id
WHERE s.product='Drama-社媒专用素材'
  AND s.category='dramawave_post'
