# Facebook Page 管理静态预览 V0.3

2026-10-19。按确认方案继续迭代 COS 静态测试页。独立于生产模块，仅维护此目录；旧版源码和 COS 地址保留。

## 入口与操作

- `dist/index.html`：视频发布。
- `dist/pages.html`：Page 管理。首次使用新版本为空；点击创建，填写文案并搜索、多选示例 Page，保存后每个 Page 单独一行。
- 已配置 Page 不能重复创建；列表单独编辑、递增模板版本。示例授权过期 Page 可以管理配置，但不可发布，原因直接展示。
- 发布只显示已配置 Page，初始不选择。全选仅覆盖已配置且授权正常的 Page；主动保存的发布草稿可在刷新后恢复。
- 切换当前 Page 独立编辑和预览。当前发布草稿不随模板保存而变化，可主动重新套用最新模板。
- 确认面板逐 Page 展示最终文案、模拟短链和可展开的长链参数。提交后冻结文案、模板版本、任务 ID、记录 ID、创建时间和链接；模板修改与后续发布不会覆盖历史记录。
- 原“预览素材”明确改为“示例剧目预览”：仅展示宏替换效果，不绑定配置或发布素材。

## 数据与边界

新存储前缀：`facebook-page-management-preview-v3-20261019`，后缀为 `:page-configs`、`:draft`、`:records`。仅在当前浏览器保存；不导入、迁移或清除旧版本的组数据。配置按 Page ID 独立保存模板和版本。本地存储写入失败时保留输入并明确报错。

示例 Page 目录和素材均是模拟数据。没有真实授权、发布、短链分配或后台 API 请求；预约仅保存演示状态，不触发真实或后台定时任务。立即发布通过浏览器计时器展示模拟结果。刷新时未完成的本地模拟任务标记取消。去重模板继续为原型占位，不处理视频。

`{name}` 是素材关联的剧名，`{desc}` 是简介，`{url}` 是模拟短链。模板要求非空、只允许以上三种精确宏、花括号完整；模板最大 16,000 UTF-8 字节。实际提交检查所引用宏的来源，替换后最多 5,000 UTF-8 字节，确认时以最终短链再次检查。示例配置的预览超长提示不代表绑定预览剧目，正式提交以实际选择素材逐 Page 校验。未关联剧集的本地视频不能使用这些剧集宏，但可使用纯文本。

## 2310 与八项归因

目标固定为 `https://www.dramawavew2a.com/ads/101/2310/view`。

规则来自当前 YouTube 视频发布归因，不采用另一个“手动生成短链”模块的规则。已通过维护技能 `references/youtube-auto-publish.md` 和 CPU 运行文件 `features/youtube_auto_publish/attribution.py` 对照；读取的运行文件 SHA-256 为 `e02e5307de3d15d205e21dd4be019048e6e02645a34b8cff7ae4833c80bdd7fb`。此原型按确认方案将平台、身份和落地页映射为 Facebook / Page / 2310。

| 参数 | 冻结来源 |
| --- | --- |
| c | `yingliang_post_CLV_VL_facebook_{PageID}*{记录创建秒时间戳}none{素材语言}*{剧名}*{标签或none}*{记录ID}` |
| af_adset | Page 名称 |
| af_adset_id | Page ID |
| af_ad | `素材名_contentid[content_id]` |
| af_ad_id | 源素材 ID |
| af_channel | 发布人邮箱映射的唯一有效 sub_user_id；原型明确使用示例 900001，不查询真实账户 |
| af_c_id | 此 Page 本次发布任务 ID |
| af_dp | 素材 content_id |

采用 UTF-8 百分号编码，空格为 `%20`，保留 `c` 中的 `*` 分隔符。任务 ID 使用本地随机 UUID；记录 ID 为本地演示序号，不申请或占用生产编号。不同 Page、不同发布分别创建短链。模拟地址采用 `https://gy.g2flow.com/s2l/fb/demo-{taskId}.html`，旁边明确标记模拟、不可访问，以纯文本展示，不生成可点击链接，也不访问该地址。

## UI 与验证

继续复用此前与 AI 后台规范对齐的 QuickNav、UiTopbar、色彩、字号、卡片和表单样式，使用离线身份。新增空列表、批量选择和独立 Page 文案交互。管理表格在小屏内横向滚动，编辑弹窗固定页头/页脚，内容区域滚动。

本地服务：在此目录运行 `python -m http.server 8783 --bind 127.0.0.1 --directory dist`。

语法/归因检查：`node --check dist/assets/app.js`、`node --check dist/assets/links.js`、`node tests/links.cjs`。

浏览器验收使用 Playwright CLI，脚本 `tests/browser-smoke.js`。从此目录执行，先创建 `output/playwright`，使用 FFmpeg 生成本地测试视频：

```powershell
ffmpeg -hide_banner -loglevel error -f lavfi -i color=c=blue:s=64x64:d=1 -c:v libvpx-vp9 -an -y output/unmapped-test.webm
npx --yes --package @playwright/cli playwright-cli -s=fbpagesv3 open http://127.0.0.1:8783
npx --yes --package @playwright/cli playwright-cli -s=fbpagesv3 run-code --filename tests/browser-smoke.js
```

脚本清理的是测试浏览器内本版本自己的演示数据；保留旧版本数据。浏览器测试驱动自动接受原生放弃编辑确认，页面发行文件不包含该覆盖。截图、网络日志和上传清单位于忽略的 `output/`；只上传 `dist/` 静态文件。

发布结果、SHA 校验和公网验收见 [VERIFICATION.md](VERIFICATION.md)。

## 回退

- 本轮基线提交 `921b7c1`，旧组版源文件 `doc/20261019.facebook-page-group-preview/` 不变。
- 旧版：[2026-10-19 Page 组预览](https://advertising-1306474899.cos.ap-hongkong.myqcloud.com/codex-artifacts/html/20261019/facebook-page-group-preview-20261019_122013/index.html)。
- 初始版：[2026-10-13 视频发布预览](https://advertising-1306474899.cos.ap-hongkong.myqcloud.com/codex-artifacts/html/20261013/facebook-publishing-preview-20261013_183108/index.html)。
- 新增不可变 COS 目录，旧地址不覆盖；回退直接使用旧地址。没有生产服务变更或重启，不需要数据库回滚。
- 维护技能上下文保持不变：本次为静态原型，无生产规则或工作流变更。
