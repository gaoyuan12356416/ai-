# Facebook 多素材与 Page 批量发布预览 V0.5

2026-10-19。在 V0.4（`8e7818c`）上新增独立静态版本，实现之前只写入方案的素材多选和素材与 Page 对应流程。只发布 COS 静态文件，不接入生产发布、短链生成、授权或素材后台 API。

## 使用流程

1. 在 Page 管理创建配置，选择 Page、剧语言和默认文案。已配置 Page 可单独编辑或删除。
2. 在视频发布点击“选择素材（可多选）”，通过复选框选择多条素材。搜索、语言筛选不会清空之前的选择；“全选 / 取消当前结果”只影响筛选结果。
3. 选择目标 Page；仅已配置、授权正常、剧语言与本批至少一条素材相同的 Page 可选。
4. 点击“按剧语言匹配”。系统在已选范围内建立同语言的素材 × Page 对应关系；每一项勾选代表一条独立任务。可手动添加或取消配对，通过语言、Page 或“只看未分配”筛选核对。
5. 切换“当前编辑配对”，分别编辑和预览文案。修改只影响当前素材与 Page 的配对，不改写 Page 模板或其他配对。
6. 选择立即开始或预约开始时间，设置同一 Page 的发布间隔，逐 Page 核对确认面板后模拟提交。

例如，两条中文素材选择两个中文 Page，一条英文素材选择一个英文 Page，匹配结果是 `2 × 2 + 1 × 1 = 5` 条任务。不会建立跨语言配对。任一已选素材未分配 Page 时，整批提交被阻止；需补充对应关系或明确移除该素材。

## 任务、文案和链接

- 每个配对独立保存文案草稿、模板版本和配置实例 ID。相同 Page 的不同素材也可使用不同文案。
- 重新匹配会提示将恢复手动取消的同语言配对，保留已有文案修改；同一素材与 Page 在一批中最多出现一次。
- 素材列表的上下箭头决定同一 Page 内的发布顺序。间隔支持 0–1440 的整数分钟；同一 Page 的第 N 条任务在起始时间后 `(N-1) × 间隔` 分钟执行。静态页仅保存时间，预约任务不会自动执行。
- 确认面板按 Page 分组，逐素材展示最终文案、独立模拟短链、长链参数、模板版本和时间。若历史有相同素材与 Page 的已发布、处理中或预约任务，必须明确勾选仍需再次发布。
- 每次确认创建独立任务 UUID、模拟记录编号和短链，并冻结素材、Page 剧语言、文案、模板版本、排序和链接。后续模板编辑、Page 删除或新一批提交不改写历史快照。
- 整批记录先一次性写入本地存储；写入失败不产生部分成功。取消预约只影响对应任务。
- `{name}` 为剧名，`{desc}` 为简介，`{url}` 为模拟短链。语法无效、宏来源缺失、替换后文案超过 5,000 UTF-8 字节均阻止提交。
- 落地页固定为 `https://www.dramawavew2a.com/ads/101/2310/view`。长链沿用 V0.4 的八项归因映射：`c`、`af_adset`、`af_adset_id`、`af_ad`、`af_ad_id`、`af_channel`、`af_c_id`、`af_dp`。每个配对的 `af_c_id` 独立，素材/Page/剧集 ID 对应本配对。UTF-8 URL 编码保留 `c` 的星号分隔符。
- 示例发布人映射 `sub_user_id=900001`、Page/素材/剧集/任务编号和短链均为模拟；短链仅作为文本展示，不能访问或占用真实编号。
- “模板 01–03”仍为待定义占位，禁选；只有“不去重”可用。历史重复提示与视频画面去重是两个不同概念。

## 存储与旧版兼容

沿用 Page 管理和历史记录前缀 `facebook-page-management-preview-v3-20261019`，保持用户已有配置和记录，不导入最早的 Page 组示例。未配置剧语言的旧 Page 需要先编辑补充语言。

批量草稿使用独立的 `:batch-draft-v5` 键；点击“保存草稿”后刷新可恢复所选素材、Page、配对、逐配对文案和发布设置。旧单素材 `:draft` 不读写或清除。删除 Page 时会清除其批量草稿引用；重新创建使用新配置实例，防止恢复被删除的旧文案。历史记录延续原型最近 200 条的本地保留上限。

本地导入视频只通过 Blob 在当前浏览器预览，不上传。未关联剧集和剧语言，因此不能参与匹配和提交，也不保存在批量草稿中；刷新后需重新选择文件。

同一浏览器、同一 COS 域名下，新旧版本共享 Page 配置和历史。旧网址可回退界面，不能撤销用户已确认的配置删除。避免同时用不同版本修改配置；不提供跨设备同步。

## 文件与验证

- `dist/index.html`：视频批量发布；`dist/pages.html`：Page 管理。
- `dist/assets/app.js`：可直接维护的静态交互逻辑。
- `dist/assets/batch.css`：批量选择、对应关系、确认面板和移动端样式。
- `tests/links.cjs`：2310、八项归因、URL 编码、宏和字节数边界。
- `tests/browser-smoke.js`：批量创建、筛选、多选、手动匹配、独立草稿、持久化、存储失败、顺序/间隔、归因、删除与历史隔离、重复确认、静态请求和桌面/平板/手机交互。

测试脚本会清理当前测试浏览器里的演示配置；只在独立 Playwright CLI 会话执行，不能使用用户实际浏览器。

```powershell
python -m http.server 8785 --bind 127.0.0.1 --directory dist
# 另一个终端，在本目录执行
node --check dist/assets/app.js
node tests/links.cjs
New-Item -ItemType Directory -Force output/playwright
ffmpeg -hide_banner -loglevel error -f lavfi -i color=c=blue:s=64x64:d=1 -c:v libvpx-vp9 -an -y output/unmapped-test.webm
npx --yes --package @playwright/cli playwright-cli -s=fbbatchv5 open http://127.0.0.1:8785/index.html
npx --yes --package @playwright/cli playwright-cli -s=fbbatchv5 run-code --filename tests/browser-smoke.js
```

发布流程：本目录源码提交并推送 GitHub后，只上传 `dist/` 到新 COS 前缀，逐文件公网读回比对长度和 SHA-256，再从公网 URL 运行交互验收。测试输出、截图和本机上传辅助脚本放在忽略的 `output/`。结果记录在 [VERIFICATION.md](VERIFICATION.md)。

回退点：`8e7818c`；[V0.4 旧页面](https://advertising-1306474899.cos.ap-hongkong.myqcloud.com/codex-artifacts/html/20261019/facebook-page-language-preview-20261019_155212/index.html)。旧源目录和旧 COS 对象不修改。没有生产服务、队列或数据库变更。维护技能上下文不变，本轮只实现静态原型。
