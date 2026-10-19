# Facebook Page 删除与剧语言预览 V0.4

2026-10-19，继续静态 COS 预览，基线 `3fa9012`。本轮实现 Page 配置删除、剧语言设置及匹配检查，澄清未定义的去重模板。多素材发布仅给出设计建议，尚未实现。

## 已实现

- Page 管理列表每行提供编辑和删除。删除弹窗明确 Page 名称/ID，确认后只删除该配置，取消当前选中及草稿引用；已有发布记录、文案、短链、预约状态保留。不会删除 Facebook 主页/帖子或取消已提交任务。
- 可重新创建删除的 Page。每次创建使用新的配置实例 ID，旧草稿不会回流到重建配置。历史任务保持原模板版本、文案和链接。
- 创建/编辑支持必填、单选“剧语言”。当前静态素材目录提供中文、英语、西班牙语；批量创建的 Page 统一选择一个语言，保存后可逐 Page 调整。
- 该语言是 Page 接收的素材所关联剧集的语言，不按 Page 名称、文案或浏览器语言推断，也不自动翻译文案。
- 发布候选仍显示全部已配置 Page。仅授权正常、已设置剧语言且与当前素材一致的 Page 可选；不一致或素材语言缺失时展示原因并禁选。全选遵守同一规则。切换素材会取消不匹配的选择，保留独立文案草稿。
- 剧语言修改递增配置版本；提交记录新增冻结的 `pageDramaLanguage` 和配置实例 ID。长链 `c` 中语言仍取素材语言。
- 模板 01–03 只是旧原型占位（原标签为构图变化、画面调整、组合处理），没有处理参数或视频生成实现。现在禁选并标注“待定义 · 暂不可用”，仅“不去重”可用。旧历史任务不改写。
- 继续使用 2310 与原八项归因、模拟短链；不调用真实短链、发布、授权或后台 API。

## 数据延续

为支持用户删除已创建结果，沿用当前 Page 管理的存储前缀 `facebook-page-management-preview-v3-20261019`。不清空已有配置，不读取旧的 Page 组存储。已有配置缺少 `dramaLanguage` 时显示“待设置”，补充后再参与匹配；原文案与历史记录保留。

新旧页面在同一浏览器/COS 源下共享这批 Page 管理配置。旧源码和网址保留用于页面回退；打开旧网址不会撤销已确认的配置删除。新旧页应避免同时编辑配置。此静态演示仍不提供跨浏览器同步。

## 入口与验证

- `dist/pages.html`：Page 管理。
- `dist/index.html`：视频发布。
- `tests/links.cjs`：2310、八字段、编码、宏和字节数边界。
- `tests/browser-smoke.js`：原 62 项场景加剧语言、删除、删除失败、重建草稿隔离、历史保留、旧数据兼容，合计 87 项。
- 使用独立 Playwright CLI 测试会话；测试脚本会清理自己的测试浏览器数据，勿在用户实际浏览器执行。

```powershell
python -m http.server 8784 --bind 127.0.0.1 --directory dist
# 另一个终端，在此目录执行
node --check dist/assets/app.js
node tests/links.cjs
New-Item -ItemType Directory -Force output/playwright
ffmpeg -hide_banner -loglevel error -f lavfi -i color=c=blue:s=64x64:d=1 -c:v libvpx-vp9 -an -y output/unmapped-test.webm
npx --yes --package @playwright/cli playwright-cli -s=fbpagesv4 open http://127.0.0.1:8784/pages.html
npx --yes --package @playwright/cli playwright-cli -s=fbpagesv4 run-code --filename tests/browser-smoke.js
```

仅上传 `dist/`。结果见 [VERIFICATION.md](VERIFICATION.md)。多素材对应建议见 [MULTI_MATERIAL_PROPOSAL.md](MULTI_MATERIAL_PROPOSAL.md)。

回退基线：`3fa9012`；[V0.3 旧版页面](https://advertising-1306474899.cos.ap-hongkong.myqcloud.com/codex-artifacts/html/20261019/facebook-page-management-preview-20261019_153146/index.html)。旧源目录未修改，不覆盖旧 COS 对象。无生产服务重启或数据库变更。维护技能上下文保持不变，本轮只调整静态原型。
