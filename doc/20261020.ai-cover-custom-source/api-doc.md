# 新入库契约

POST https://aa.yingliangads.com/api/material/source，Authorization: Bearer服务端AD_MATERIAL_SOURCE_API_TOKEN；单张JSON对象。必填app_id/user_id/initiator为整数；content_id/country/language/content_sign/url/name为字符串，category为实际比例，remark存来源引用。

固定语言缺省国家：enUS esES ptPT frFR deDE itIT plPL ruRU trTR arSA jaJP koKR idID thTH viVN csCZ elGR roRO zh-TW/TW zh-CN/CN。有效ISO国家保留（VI是合法地区，不能仅按语言改写）；CS无效时按cs→CZ。

数据签名=SHA256(ai-cover-source-v1:规范JSON(source_kind/source_key/app_id/sha256))。首次请求冻结；同签名元数据漂移拒绝。

data.id单独保存api_id；正式库通过content_sign唯一回读，验证真实产品、content_id(data_source_id)、type1、人员、比例、language、name；保存final_id，不混同接收/正式ID。

2026-10-20用户确认：入库程序用剧库country覆盖请求country，剧库为空时正式country也为空，这是有效成功结果。请求仍按原规则保留/补齐国家；正式country保留实际值，不要求与请求一致，也不写回素材库。confirmed事件保存country_policy=drama_catalog_override、requested_country和formal_country。语言及其他元数据验证不放宽。

HTTP403不解析JSON即明确拒绝；用户于2026-10-20确认业务code403表示产品被限制，因此按明确业务拒绝记录。-1为业务拒绝，重复签名走对账；-2/非JSON/缺id/超时不确定。成功码无固定值约束。日志仅固定安全字段与响应hash，不记录token或任意响应文本。

产品限制解除后的同请求重试必须由人工明确授权，先持久HOLD、查签名确认未正式入库，再用recover.py classify/rearm记录证据引用；原请求和签名不变，attempts累计。未知超时不能使用以前尝试的403回执重新授权。工具自身不POST；每次授权仅允许下一次prepared出站。已有正式素材字段不符时只reconcile，禁止重传。
