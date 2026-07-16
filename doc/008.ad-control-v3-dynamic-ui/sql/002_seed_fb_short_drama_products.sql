-- Exact custom_source_insight.product enums verified read-only on 2026-07-16.
-- No fuzzy names. `[w2a]FreeReels-double` is explicit because app_type inference misses it.

INSERT INTO `ads_ai`.`ad_control_v3_product_catalog`
(`channel`,`product_value`,`canonical_product`,`product_type`,`source_app_ids_json`,`evidence_json`,`enabled`,`created_by_user_id`,`updated_by_user_id`,`created_at`,`updated_at`)
VALUES
('facebook','Drama Suagr','Drama Suagr','short_drama',JSON_ARRAY(),JSON_OBJECT('source','custom_source_insight_exact_enum','verified_on','2026-07-16'),1,'migration','migration',UTC_TIMESTAMP(6),UTC_TIMESTAMP(6)),
('facebook','Drama-AI素材','Drama-AI素材','short_drama',JSON_ARRAY(),JSON_OBJECT('source','custom_source_insight_exact_enum','verified_on','2026-07-16'),1,'migration','migration',UTC_TIMESTAMP(6),UTC_TIMESTAMP(6)),
('facebook','Drama-B','Drama-B','short_drama',JSON_ARRAY(),JSON_OBJECT('source','custom_source_insight_exact_enum','verified_on','2026-07-16'),1,'migration','migration',UTC_TIMESTAMP(6),UTC_TIMESTAMP(6)),
('facebook','Drama-C','Drama-C','short_drama',JSON_ARRAY(),JSON_OBJECT('source','custom_source_insight_exact_enum','verified_on','2026-07-16'),1,'migration','migration',UTC_TIMESTAMP(6),UTC_TIMESTAMP(6)),
('facebook','Drama-comics','Drama-comics','short_drama',JSON_ARRAY(),JSON_OBJECT('source','custom_source_insight_exact_enum','verified_on','2026-07-16'),1,'migration','migration',UTC_TIMESTAMP(6),UTC_TIMESTAMP(6)),
('facebook','Dramawave','Dramawave','short_drama',JSON_ARRAY(),JSON_OBJECT('source','custom_source_insight_exact_enum','verified_on','2026-07-16'),1,'migration','migration',UTC_TIMESTAMP(6),UTC_TIMESTAMP(6)),
('facebook','dramawaveminis','dramawaveminis','short_drama',JSON_ARRAY(),JSON_OBJECT('source','custom_source_insight_exact_enum','verified_on','2026-07-16'),1,'migration','migration',UTC_TIMESTAMP(6),UTC_TIMESTAMP(6)),
('facebook','FreeReels','FreeReels','short_drama',JSON_ARRAY(),JSON_OBJECT('source','custom_source_insight_exact_enum','verified_on','2026-07-16'),1,'migration','migration',UTC_TIMESTAMP(6),UTC_TIMESTAMP(6)),
('facebook','MoboShort','MoboShort','short_drama',JSON_ARRAY(),JSON_OBJECT('source','custom_source_insight_exact_enum','verified_on','2026-07-16'),1,'migration','migration',UTC_TIMESTAMP(6),UTC_TIMESTAMP(6)),
('facebook','bestreels','bestreels','short_drama',JSON_ARRAY(),JSON_OBJECT('source','custom_source_insight_exact_enum','verified_on','2026-07-16'),1,'migration','migration',UTC_TIMESTAMP(6),UTC_TIMESTAMP(6)),
('facebook','hotdrama','hotdrama','short_drama',JSON_ARRAY(),JSON_OBJECT('source','custom_source_insight_exact_enum','verified_on','2026-07-16'),1,'migration','migration',UTC_TIMESTAMP(6),UTC_TIMESTAMP(6)),
('facebook','DramawaveSource','DramawaveSource','short_drama',JSON_ARRAY(),JSON_OBJECT('source','custom_source_insight_exact_enum','verified_on','2026-07-16'),1,'migration','migration',UTC_TIMESTAMP(6),UTC_TIMESTAMP(6)),
('facebook','RealReel','RealReel','short_drama',JSON_ARRAY(),JSON_OBJECT('source','custom_source_insight_exact_enum','verified_on','2026-07-16'),1,'migration','migration',UTC_TIMESTAMP(6),UTC_TIMESTAMP(6)),
('facebook','Cafedrama','Cafedrama','short_drama',JSON_ARRAY(),JSON_OBJECT('source','custom_source_insight_exact_enum','verified_on','2026-07-16'),1,'migration','migration',UTC_TIMESTAMP(6),UTC_TIMESTAMP(6)),
('facebook','[w2a]FreeReels-double','FreeReels','short_drama',JSON_ARRAY(),JSON_OBJECT('source','explicit_w2a_catalog','verified_on','2026-07-16','reason','app_type_inference_misses_landing_product'),1,'migration','migration',UTC_TIMESTAMP(6),UTC_TIMESTAMP(6))
ON DUPLICATE KEY UPDATE
  `canonical_product`=VALUES(`canonical_product`),
  `product_type`=VALUES(`product_type`),
  `evidence_json`=VALUES(`evidence_json`),
  `updated_by_user_id`='migration',
  `updated_at`=UTC_TIMESTAMP(6);
