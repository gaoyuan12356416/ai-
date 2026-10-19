# V0.5 验证与发布记录

范围：新增 Facebook 多素材与 Page 批量发布静态预览，基线 `8e7818c`。保留所有旧源目录和 COS 地址；不调用生产发布或短链服务。

## 本地验证

- `node --check dist/assets/app.js` 和 `node tests/links.cjs` 通过。
- 本地最终一轮 95 项交互检查通过，55 个请求中非静态请求 0、浏览器运行错误 0。
- 独立 Playwright CLI 会话验证素材多选、跨搜索/语言筛选保留选择、取消选择弹窗不覆盖原选择。
- 两条中文素材 × 两个中文 Page，加一条英文素材 × 一个英文 Page，共 5 条任务；跨语言禁选、手动调整和未分配阻止提交通过。
- 逐配对文案、模板重新应用隔离、保存草稿/刷新、旧单素材草稿保留、整批存储失败不部分成功、模板编辑与 Page 删除不改写历史通过。
- 每个配对的独立任务 ID、模拟短链和 2310 八字段归因核对通过；中文 Page 内素材顺序与 10 分钟间隔正确。
- 非法宏、替换后 UTF-8 超长、无剧语言的本地素材、过期预约和错误间隔均阻止提交。重复配对必须明确确认；单条取消不影响同批其他任务。
- 桌面 1440、平板 820、手机 390 像素宽度检查页面与弹窗，无横向溢出。已人工查看多选弹窗、对应关系与手机确认面板。
- 自动化驱动在“只看未分配”下使用点击后检查列表消失，因为成功匹配后该复选框会退出筛选结果；截图等待移动端侧栏收起动画完成。

## 发布记录

源码提交 `5e8a427095e69b504f1f3e54aa8c7d9d5a7b0ba1`，分支 `codex/fb-batch-publishing-preview-20261019`；先推送 GitHub，再发布新 COS 前缀。

- [视频批量发布](https://advertising-1306474899.cos.ap-hongkong.myqcloud.com/codex-artifacts/html/20261019/facebook-batch-publishing-preview-20261019_163205/index.html) / [Page 管理](https://advertising-1306474899.cos.ap-hongkong.myqcloud.com/codex-artifacts/html/20261019/facebook-batch-publishing-preview-20261019_163205/pages.html)。
- 16 个静态文件逐一公网读回，全部 HTTP 200，长度和 SHA-256 与本地一致。index.html HEAD：200 / text/html / 26,980 字节。
- index.html SHA-256：`f09ea85c9b9a4b58a4e30d9109fd8adad16842fba8c9cc900c682a24a65e0348`。
- 公网独立浏览器复验 95 项交互检查通过，55 个请求中非静态请求 0、运行错误 0。包含桌面、平板和手机布局检查，所有测试夹具已清理。
- 完整校验和及交互检查名见 [public-verification.json](public-verification.json)；本地检查名见 [local-verification.json](local-verification.json)。
- 回退基线 `8e7818c` 的旧源目录无改动；[V0.4 旧地址](https://advertising-1306474899.cos.ap-hongkong.myqcloud.com/codex-artifacts/html/20261019/facebook-page-language-preview-20261019_155212/index.html) 继续保留。只发布 dist 中静态文件，未覆盖旧 COS 对象。

本轮维护技能上下文保持不变；没有服务重启、生产数据或真实发布动作。
