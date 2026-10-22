# Page 配置搜索全部有权限的已授权 Page

用户报告的十个 Page 在 `ads_facebook_page_post` 有授权记录，但没有任何
`ads_facebook_page_group_ins` 成员记录。旧来源从 Dramawave Page 池开始关联，
所以管理员也搜不到。此次候选改从授权表开始读取。

## 权限与兼容性

- 管理员沿用全局范围；普通用户通过可信飞书邮箱唯一映射的 business_owner_id，
  匹配 `ads_facebook_info.user_id → facebookUserID → ads_facebook_page_post.fb_user_id`。
  Facebook 身份严格按 BINARY 比较，不能用 BM 身份推断，也不接受浏览器自报身份。
- 保留原有效 Dramawave Page 池负责人的访问授权，作为与个人授权并列的可选来源。
  现网 owner 841 的待发布任务依赖此权限；移除它会使旧任务失去访问权。
  新个人授权 Page 无须加入任何池，也不要求产品归属。
- 仅个人授权访问时，名称、语言、可用授权数量均只从本人授权记录聚合；不会因
  别人的有效 Token 而把本人的失效授权判为可用。原池授权继续可用同 Page 公共授权。
- 保留 `status<>1 AND TRIM(page_access_token)<>''` 资格规则；无可用授权的已授权
  Page 显示禁用原因。该判断不是一次新的 Graph Token 有效性检测。
- 搜索、配置访问、创建/编辑、预检、Excel、池调度和 worker 执行前读取共同使用
  SourceRepository。当前凭证轮换/个人 Token 模式、租户隔离、任务/短链/额度不变。
- 完整数字 Page ID 在 SQL 中精确匹配；名称搜索使用参数化 LOCATE，不把 %/_ 当通配符。
  授权表按真实 Page ID 聚合，多个授权不重复显示，兼容字段 group_ids 返回空数组。
  查询保留 8 秒上限、20,000 结果边界和 200 ID 分批；搜索在结果限额之前执行。

## 验证

`tests/test_fb_authorized_pages.py` 在独立关系型夹具上执行真实查询谓词，覆盖无池
个人授权、跨用户拒绝、大小写身份隔离、管理员、重复授权、失效/空授权、实时撤销、
配置访问及原池授权保留。查询同时通过真实 SQL broker 白名单解析。
既有 source/link、broker、core、Page pool、gateway、Excel 和部署器回归亦需通过。
线上验收只查询这十个 Page、普通用户范围和既有待发布任务权限；不创建配置或发布测试帖。

## 发布和回退

基线 `429932cfdcbfc69d5544835e853285216bce5eae` 的 sources.py Git blob 与线上两份
文件 SHA256 一致：`0dd7fbd2226e94b7c5b8a21820ac52b1f216e9595195707f536bb2f0a13e5b0d`。
部署入口 `scripts/deploy_fb_authorized_pages.py` 仅允许替换
`/opt/fb-auto-post/current/features/fb_video_publish/sources.py` 和
`/root/drama_material_service/features/fb_video_publish/sources.py`。
先验证 exact pushed GitHub ref、文件基线和数据盘，再保存服务状态、备份、排空、
窄重启 FB sidecar 并恢复原定时器/worker 状态；不重启主 API，不迁移数据库或改静态页。

执行 `python3 scripts/deploy_fb_authorized_pages.py` 演练；通过后追加 `--apply`。
回退必须使用同一 release 下的此脚本 `--rollback <本次 manifest.json>`。
**旧来源不能处理新接收的未入池 Page，因此有待执行/unknown 任务或启用素材池时，
回退入口拒绝恢复旧代码。先完成任务对账和计划处理，再回退；不能清库或恢复旧 SQLite。**
现有配置、任务和短链保留；回退后未入池 Page 的新搜索/配置访问会回到旧限制。

部署结果、准确 manifest 路径和线上验证数据在验收后补充。
