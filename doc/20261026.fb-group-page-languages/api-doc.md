# 接口变化

沿用 /api/fb-video-publish 与 fbPostMaterialPool 权限。

- GET /material-pool/schedule 的pool增加 page_language_selection 和 routing_policy_version。
- POST /material-pool/schedule 与 /material-pool/preview 支持 page_language_selection={mode:all|selected,codes:[规范语言],include_blank:boolean}。all时codes为空；selected空codes仅在include_blank=true时有效。schedule保留operation_id/expected_version。
- 新 POST /material-pool/pages/preview 只读：group_ids、page_language_selection、q（可选）、cursor（可选）、limit（默认50，最大100）。返回items（Page ID/名称/语言/类型/组/可用状态/原因）、language_summary、total、eligible_count、excluded_count、matching_count、has_more、next_cursor。q仅过滤展示，total等范围计数不受q影响。
- language_summary含筛选前去重成员分布items[{language,language_kind,page_count}]及total_page_count/conflict_page_count/invalid_page_count/missing_page_count。unrestricted代表真空Page。
- 配对language继续表示素材语言，新增page_language、language_kind用于解释Page限制；不使用空Page语言替代素材语言或归因语言。

接口不接收客户端actor；从当前可信登录身份核验组权限。目标预览不写库、不占用、不发帖。
