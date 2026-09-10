# YouTube 工作台闪屏修复

用户录屏20260910182630_rec_.mp4长21.53秒、1918×952。原因见bugs/BUG-008.md。仅更新static/youtube-publish.js和HTML版本号。CSS入场动画保留用于真正打开弹窗；后台刷新保留节点，不重复执行入场动画。素材SQL、AI参考图生成、飞书/YouTube发布后端均未变。

## 验证

Playwright CLI独立本机mock API，先运行69327e8旧版，再运行补丁；真实6秒计时未加速。旧版两轮产生4次动画，弹窗/封面重建；补丁专项29项通过，动画0次、真实进度继续显示、相同媒体节点保持。输入焦点/选区、父子弹窗、迟到素材查询和320px滚动位置均验证。视频专项验证节点保留，未将空测试视频标为真实播放成功。

既有发布34、原剧参考43、加载并发10，共116项浏览器检查通过。1366×768、1920×1080截图已复核，位于output/playwright/flicker-recording/。Node/Python语法、diff、功能guard通过；部署脚本临时目录7项验证通过；独立JS复核无阻断问题。所有API采用本机mock，没有真实发布、消息或生图。

## 部署边界

分支codex/youtube-flicker-fix-20260910，基于570f4c4（线上运行69327e8）。scripts/deploy_youtube_flicker_fix.py从GitHub准确提交归档安装JS/HTML至/root/drama_material_service/static及/usr/share/nginx/html，共4文件。检查数据盘UUID、基线SHA、备份manifest，先JS后HTML原子替换，验证两份字节与feature guard。不重启API、worker或Nginx，不使用任务空闲门槛，不中断生成中的用户任务。

部署前18:30只读快照：HTML faddceb3c861ac44b1cd6794102a93c6f3f0c781185a7e1a3630065973ef06c3，JS a3e1df0d87f749598a1190f806463bc0c6d8731d546746a21a4ac7cfec325d68。五服务active/0重启，账本6、短链7，有1条用户任务生成中。SQL SHA c301d02c6bfad8fa62b86cc2befe83176a38616c4d42c9e385bfb78e0e838e01。

准确运行提交、备份、公网回读及回滚命令在切换后追加。新JS版本20260910-flicker-v1。
