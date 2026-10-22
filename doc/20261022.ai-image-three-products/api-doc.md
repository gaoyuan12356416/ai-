# 静态接口 v3

无在线数据库API。内部report.json含schema_version=3、report_date/generated_at/period/products/app_scope/validation/views，view键all/1479/2373/3360。

view.summary是昨日production/performance；view.week是七日production/performance/comparisons/groups/insights。trend七日。production包含new_count/new_image_count/backfill_count/migration_count/historical_backfill_count/dramas/total_asof。metrics沿用spend/af_revenue_d0/roas/impressions/clicks/installs/af_installs/purchases/ctr/cpi/spending_materials。

素材新增source_product_id/name、delivery_product_ids/names、is_source_product、week指标、thumbnail_url。单产品明细包含来源库存及跨产品投放使用的素材，效果仅为所选投放产品，产出仅is_source_product。总计按正式ID去重。

build_bundle(report,history,base_url,version)返回relative COS key -> {data:bytes,content_type,immutable,compress}。首页剥离完整明细/分组；assets/VERSION/details-VIEW.json返回groups/week_groups/materials/unresolved。history.json独立刷新，日页绑定生成版本。gzip对象Content-Encoding与Content-Type匹配；原文与存储哈希均冻结，公开读回验证。新prefix之外仅允许旧精确index.html跳转。

