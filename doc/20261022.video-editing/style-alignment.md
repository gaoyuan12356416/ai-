# 视频剪辑页面样式规范修正

## 范围与依据

用户反馈视频剪辑页面风格偏离 AI 后台，要求严格遵循 `ai-backend-maintenance`。以 CPU 正式发布的 `x-post-material-pool.html` 和共享 `ui-topbar.css` 为视觉基线；同时核对正式 `fb-auto-publish-templates.html`。不以本地旧页面的内容作为线上现状。

仅调整 `static/video-editing.html`、`static/video-editing.css` 和 `static/video-editing.js` 的两处空态文案。保留全部 94 个 ID、9 个 data 绑定、共享组件调用和剪辑业务逻辑。

## 统一规则

- 品牌使用“AI自动后台 / ai.yingliangads.com”，桌面侧栏 270px，深蓝渐变，蓝绿 AI 标记；侧栏由 `QuickNav.render` 渲染。
- 页面主色为 `#2563eb`，正文、弱文字、边框、背景、标题字号、按钮和卡片圆角对齐既有后台。
- 顶部用户卡片、登录退出、刷新复用 `UiTopbar`。移除视频页对顶部布局和用户卡片宽度的覆盖，窄屏仍显示用户卡片与刷新入口。
- 工作台保留项目内容、视频预览、片段设置和时间线。移除宣传式英文装饰、页签编号和“故事”文案；实际视频区域保留黑底。
- 窄屏侧栏按既有后台规则堆叠显示，控件换行；长时间线在自身区域滚动，禁止以隐藏导航或用户信息换取空间。
- 三份共享静态资源不修改；HTML 的视频页 CSS/JS 版本号改为 `20261022-standard-ui-2`。

## 验证

- `node --check static/video-editing.js`、`node --check static/quick-nav.js` 通过。
- `node --test tests/test_video_editing_frontend.js tests/test_video_editing_browser_contract.js`：23/23 通过。
- 技能要求的主服务及 worker Python 编译检查通过；这些 Python 文件未修改。
- 新旧 HTML 的 ID/data 绑定集合一致，无重复 ID。
- 独立审查确认 HTML/JS 只有预期结构/文案变化；CSS 的上传、时间线、双播放器、状态、弹窗和隐藏规则完整，未覆盖共享导航或顶部用户卡布局。
- Chrome 本地模拟模式：1440px 工作台、820px 素材选择、390px 工作台/素材库/模拟任务/剧集选择弹窗检查通过。页面无横向溢出，剧集弹窗 `clientWidth=scrollWidth=335px`；窄屏侧栏、用户卡和刷新按钮保持显示。模拟任务没有请求 GPU。
- 1440px 实测侧栏 270px、主按钮 `rgb(37, 99, 235)`。浏览器旧 CSS 缓存通过新版 CSS/JS 查询版本 `20261022-standard-ui-2` 更新；临时测试 viewport 已重置。
- 本地控制台只有此前浏览器扩展消息通道错误，未看到新模块脚本错误。正式环境回读见下文。

## 部署与回滚边界

先提交推送 GitHub，再在 CPU `43.166.187.96` 的干净候选目录 fetch 并 checkout 确切提交。验证数据盘 UUID、现网文件哈希和候选源码哈希；备份后只替换以下两个目录中的 `video-editing.html/css/js`，每个文件使用临时文件和原子替换：

- `/usr/share/nginx/html`
- `/root/drama_material_service/static`

不修改 Nginx 配置、数据库、任务账本、COS 对象和 GPU 程序，无需停止、重启或重新提交制作。更新前后比较 API、两类制作 worker 和发布服务的进程身份，回读正式 HTTP 内容并核对 Git 提交的 SHA256。

回滚只从本次备份恢复这六个静态文件，保留兼容新旧任务的调度器和全部业务记录。具体备份目录及命令见下文。

技能上下文保持不变：本次是落实已有共享导航/顶部栏规范，没有新增任务语义或可复用例外。


## 正式发布回执

2026-10-22 14:48（北京时间），GitHub 分支 `codex/video-editing-standard-ui-20261022` 推送成功，CPU 候选目录 fetch 并 checkout 提交 `866e9d6e4d4a5b7578999fcf65de38b933ac3948`，随后完成上述六个静态文件原子替换。

- 线上三个文件均 HTTP 200，响应 SHA256 与 Git 提交一致；两份静态目录一致。
- `quick-nav.js`、`ui-topbar.css`、`ui-topbar.js` 及两份参考页面哈希保持不变。
- 五个受保护服务均保持 active 且 PID 不变：API `3770172`，剪辑 coordinator `3797300`，旧任务 worker `3797297`，发布服务 `2937249` / `2576490`。没有 restart/reload，没有变更 GPU。
- Chrome 正式登录状态通过；当前视频剪辑导航组展开、站内链接全部为根路径；侧栏实测 270px、主按钮 `rgb(37, 99, 235)`、用户卡片显示正常，默认 2560px 视口无页面横向溢出。
- 共享素材库仍有原可剪辑素材。打开既有项目显示“已保存 · v16”、两部剧独立时间线（12 秒 / 9.7 秒），两份原成片仍为“制作完成”并显示预览、下载入口。本轮未点击制作、重试或提交新项目。
- 正式截图：本地 `output/video-editing-style-validation/production-editor.png`、`production-tasks.png`、`production-library.png`。截图和详细验收回执不提交 Git。

| 文件 | 发布 SHA256 |
| --- | --- |
| `video-editing.html` | `5a6d1767dec9fc86a43665555b69ae5e1c4e56d25a90d64e9a00957271ac0b2b` |
| `video-editing.css` | `2305418ec7dfb2ca71b35a10118ee0ba07e085adb735ea3f65e9c8681eae7d17` |
| `video-editing.js` | `d47d6d58be040273ea0360b2c67511e1146f81535e4c1848db2059f88acf36e9` |

备份：`/mnt/data-disk/video-editing/backups/style-20261022T064832Z-866e9d6e4d4a`。该目录保留两组原始文件、SHA256/进程清单及回滚脚本；回滚脚本会先拒绝覆盖后续已变化的页面。

在 CPU 上执行以下命令仅回退本次六个静态文件，无需重启服务：

```bash
python3 /mnt/data-disk/video-editing/backups/style-20261022T064832Z-866e9d6e4d4a/rollback.py
```
