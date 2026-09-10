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
  '1479' AS app_id
FROM `kunlunads_dev`.`ads_custom_source` s
WHERE s.data_source='6'
  AND s.task_id='-2'
  AND s.product='dramawave'
  AND s.designer='789'
  AND s.created_at>='2026-09-10'
