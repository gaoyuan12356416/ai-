SELECT HEX(JSON_OBJECT(
  'id', CAST(uploaders.user_id AS CHAR),
  'name', COALESCE(NULLIF(TRIM(u.name), ''), NULLIF(TRIM(u.username), ''),
    CASE WHEN uploaders.user_id=0 THEN '未知上传人' ELSE CONCAT('用户 ', uploaders.user_id) END)
))
FROM (SELECT DISTINCT user_id FROM `kunlunads_dev`.`ads_custom_source`) uploaders
LEFT JOIN `kunlunads_dev`.`admin_users` u ON u.id=uploaders.user_id
WHERE EXISTS (
  SELECT 1 FROM `kunlunads_dev`.`ads_custom_source` s
  WHERE s.user_id=uploaders.user_id AND s.type=2
)
ORDER BY COALESCE(NULLIF(TRIM(u.name), ''), NULLIF(TRIM(u.username), ''),
  CASE WHEN uploaders.user_id=0 THEN '未知上传人' ELSE CONCAT('用户 ', uploaders.user_id) END), uploaders.user_id
