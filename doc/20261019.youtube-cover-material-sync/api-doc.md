# 入库协议
POST https://aa.yingliangads.com/api/material/ai-source
服务端配置 AI_SOURCE_CALLBACK_TOKEN，兼容现有 Authorization/Authrization 两个Bearer请求头；不把Token保存到请求账本。

请求JSON数组最多50项，每项 app_package=com.dramawave.app、material_id=稳定60位整数、content_id、category、name、url、email=映射后的后台username、created_at=原资产北京时间。

HTTP200/code0仅代表接收。先查 data_source=102 / data_source_id=material_id；remark为正式素材ID时按主键读回。正式记录必须 product=Drama-AI素材、type=1、data_source=6、data_source_id=content_id，且名称、分类、user_id/designer/initiator与冻结参数一致。元数据以及最终ID保存到独立SQLite。
