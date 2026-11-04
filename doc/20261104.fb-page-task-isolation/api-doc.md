# API 合同

保持既有 `/api/fb-video-publish` 与内部 pipeline API，不增加绕过检查的前端开关。

- Excel / 手动预检与提交：不同素材不因同 Page 旧 unknown 被拒；同素材未决仍拒，文案明确素材范围。
- 幂等 ID、预览一致性、Page 权限、凭证范围、预约与媒体校验不变。
- 已有 Graph ID 只 GET 对账，不通过重新 POST 获取新 ID。
- 原 Excel skipped 元数据保留历史事实。用户重新上传完整原配置，已接受行仍由原重复规则保护。
- 内部资源锁变化不改变身份、短链和既有成功发布记录。
