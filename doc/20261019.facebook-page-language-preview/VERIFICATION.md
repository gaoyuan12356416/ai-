# V0.4 验证与发布记录

2026-10-19。范围仅新增静态预览目录；旧版源文件与 COS 地址保留。

## 本地结果

- `node --check dist/assets/app.js` 与八字段归因/宏测试通过。
- Playwright CLI：87 项交互检查通过，109 个请求中无非静态请求，浏览器运行错误为 0。
- 新增覆盖：必填剧语言、批量保存、语言切换与匹配示例、禁选不匹配 Page、切换素材清理选择、全选语言约束、英文归因语言、明确删除确认与取消、存储失败不删除、指定 Page 删除、刷新持久化、重建新实例隔离旧草稿、删除最后一个配置恢复空列表、删除后历史和短链可查看、旧 V0.3 配置未设置语言的兼容流程。
- 原有独立文案、冻结历史、2310 八项归因、非法宏/UTF-8 长度、来源缺失、授权异常、存储失败保护继续通过。
- 1440 / 820 / 390 像素宽度无页面横向溢出；编辑弹窗与长链不溢出。桌面创建/删除弹窗和手机编辑页已人工查看。
- 测试驱动修正一处折叠长链的读取方式：使用 textContent 检查隐藏参数，发行页业务逻辑不受影响。

## COS 与公网结果

源码提交 `9f6aa0987a5c13573739c035fd0f050d297dc98e`，分支 `codex/fb-page-language-preview-20261019`，已推送 GitHub 后发布。

- [Page 管理](https://advertising-1306474899.cos.ap-hongkong.myqcloud.com/codex-artifacts/html/20261019/facebook-page-language-preview-20261019_155212/pages.html) / [视频发布](https://advertising-1306474899.cos.ap-hongkong.myqcloud.com/codex-artifacts/html/20261019/facebook-page-language-preview-20261019_155212/index.html)。
- 上传并逐文件公网读回 15 个静态文件，全部 HTTP 200、长度与 SHA-256 与本地完全一致。HTML HEAD：200 / text/html / 25,167 字节。
- index.html SHA-256：`cf0e24145ebbcec8f0cf2ebc9fa41aacce29e9231452f64d6cf1c4a016abb220`。
- 公网浏览器复验 87 项通过；109 个请求，非静态请求 0、运行错误 0；含 1440 / 820 / 390 像素布局检查。
- 完整检查名和文件校验值见 [public-verification.json](public-verification.json)。测试完成后清理独立测试浏览器本版夹具数据。
- 基线 `3fa9012` 的旧源目录无变动；旧 COS 地址保留。只上传 dist，未修改服务器程序、生产发布或短链状态。
- 多素材对应仍为方案文档，未作为已实现能力发布。技能上下文保持不变。
