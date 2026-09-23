# API

Prefix /api/youtube-auto-publish. All responses are private JSON no-store. Cookie + youtube_auto_publish module + youtubeAutoPublish navigation required. POST also uses the existing same-origin JSON validation and 32 KiB limit. Caller-supplied identity fields are discarded.

- GET /short-links/channels?refresh=0|1: existing safe channels/checking/error DTO, no scopes/account credentials. Independent of material SQL.
- GET /short-links/dramas?search=...&page=1: {items:[{content_id,language,name,selectable}],page,has_more,search_required}; page 1..1000, page size 50, at least 2 search characters, maximum 200. Name prefix or exact ID; duplicate episode metadata collapses. Empty search does not query MySQL.
- POST /short-links: {operation_id: canonical UUID,channel_local_id: decimal string,content_id,language}. Returns {link}.
- GET /short-links/{operation_id}: owner/tenant-scoped {link}, 404 if absent (including another owner).

link fields: operation_id,status (pending/failed/published),short_url,channel_name,drama_name,content_id,language,created_at,message. short_url is populated only after immutable file readback. Same UUID with different selection returns 409. Failed or unknown POST retains UUID and original selection; GET checks status and POST retries the same frozen identity. Explicit new generation uses a new UUID.

Attribution (V2, new operations): destination https://www.dramawavew2a.com/ads/101/2284/view; af_adset=channel name; af_adset_id=real channel ID; af_ad=drama name_contentid[ID]; af_ad_id=yt_manual; af_channel=mapped sub_user_id; af_c_id=UUID (operation_id only); af_dp=content_id. c=yingliang_post_CLV_VL_youtube_CHANNEL*UNIXSECONDSnoneLANGUAGE*DRAMA*none*manual_LINK_ID. UTF-8 percent encoding preserves star separators. URL path https://gy.g2flow.com/s2l/youtube/ID.html.

Existing V1 operations retain af_ad_id=none and af_c_id=yt_manual_UUID, including retries after failure or response loss. Attribution version is frozen in context_json. Published files and automatic publication attribution are unchanged.
