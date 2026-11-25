SELECT s.id AS id, s.name COLLATE utf8mb4_general_ci AS name, s.url AS url, COALESCE(s.cover,'') AS thumbnail_url,
       s.tag_name AS content_id, '' AS source_job_id, '' AS source_kind,
       s.name AS macro_name, '' AS macro_desc,
       COALESCE(s.language,'') AS language,
       CASE WHEN s.video_duration>0 THEN CONCAT(FLOOR(s.video_duration/60),':',LPAD(MOD(s.video_duration,60),2,'0')) ELSE '' END AS duration,
       '' AS size, 1479 AS app_id, u.id AS uploader_id, COALESCE(NULLIF(u.name,''),u.username) AS uploader_name,
       s.is_delete AS intake_deleted, COALESCE(s.remark,'') AS receipt,
       f.id AS receipt_id
  FROM kunlunads_dev.ads_custom_source s
  JOIN kunlunads_dev.admin_users u ON s.designer=u.username AND BINARY s.designer=BINARY u.username
  LEFT JOIN kunlunads_dev.ads_custom_source f
    ON f.id=CAST(NULLIF(s.remark,'') AS UNSIGNED) AND BINARY CAST(f.id AS CHAR)=BINARY s.remark
   AND f.data_source=6 AND f.type=2 AND f.is_delete=0 AND f.user_id=u.id
   AND BINARY f.data_source_id=BINARY s.tag_name AND f.category=s.category
 WHERE s.data_source=102 AND s.type=2 AND s.user_id=0 AND s.product='Dramawave'
   AND s.category='dramawave_post' AND s.url LIKE 'https://marketing.tianmai.cn/materialcopy/%'
