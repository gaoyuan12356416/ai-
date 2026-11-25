SELECT HEX(JSON_OBJECT('id',CAST(uploaders.user_id AS CHAR),
  'name',COALESCE(NULLIF(TRIM(u.name),''),NULLIF(TRIM(u.username),''),
  CASE WHEN COALESCE(u.id,0)=0 THEN '未知上传人' ELSE CONCAT('用户 ',uploaders.user_id) END)))
FROM (
  SELECT known.user_id
  FROM (SELECT DISTINCT user_id FROM kunlunads_dev.ads_custom_source) known
  WHERE EXISTS (SELECT 1 FROM kunlunads_dev.ads_custom_source s WHERE s.user_id=known.user_id AND s.type=2)
  UNION
  SELECT editor.id FROM kunlunads_dev.ads_custom_source s
  JOIN kunlunads_dev.admin_users editor ON editor.username=s.designer AND BINARY editor.username=BINARY s.designer
  WHERE s.is_delete=0 AND s.data_source=102 AND s.product='Dramawave' AND s.category='dramawave_post'
    AND s.type=2 AND s.user_id=0 AND s.url LIKE 'https://marketing.tianmai.cn/materialcopy/%'
) uploaders
LEFT JOIN kunlunads_dev.admin_users u ON u.id=uploaders.user_id
ORDER BY COALESCE(NULLIF(TRIM(u.name),''),NULLIF(TRIM(u.username),''),CAST(uploaders.user_id AS CHAR)),uploaders.user_id
