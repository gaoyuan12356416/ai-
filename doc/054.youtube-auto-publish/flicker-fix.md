# YouTube 工作台闪屏修复

用户录屏20260910182630_rec_.mp4长21.53秒、1918×952。原因见bugs/BUG-008.md。仅更新static/youtube-publish.js和HTML版本号。CSS入场动画保留用于真正打开弹窗；后台刷新保留节点，不重复执行入场动画。素材SQL、AI参考图生成、飞书/YouTube发布后端均未变。

## 验证

Playwright CLI独立本机mock API，先运行69327e8旧版，再运行补丁；真实6秒计时未加速。旧版两轮产生4次动画，弹窗/封面重建；补丁专项29项通过，动画0次、真实进度继续显示、相同媒体节点保持。输入焦点/选区、父子弹窗、迟到素材查询和320px滚动位置均验证。视频专项验证节点保留，未将空测试视频标为真实播放成功。

既有发布34、原剧参考43、加载并发10，共116项浏览器检查通过。1366×768、1920×1080截图已复核，位于output/playwright/flicker-recording/。Node/Python语法、diff、功能guard通过；部署脚本临时目录7项验证通过；独立JS复核无阻断问题。所有API采用本机mock，没有真实发布、消息或生图。

## 部署边界

分支codex/youtube-flicker-fix-20260910，基于570f4c4（线上运行69327e8）。scripts/deploy_youtube_flicker_fix.py从GitHub准确提交归档安装JS/HTML至/root/drama_material_service/static及/usr/share/nginx/html，共4文件。检查数据盘UUID、基线SHA、备份manifest，先JS后HTML原子替换，验证两份字节与feature guard。不重启API、worker或Nginx，不使用任务空闲门槛，不中断生成中的用户任务。

部署前18:30只读快照：HTML faddceb3c861ac44b1cd6794102a93c6f3f0c781185a7e1a3630065973ef06c3，JS a3e1df0d87f749598a1190f806463bc0c6d8731d546746a21a4ac7cfec325d68。五服务active/0重启，账本6、短链7，有1条用户任务生成中。SQL SHA c301d02c6bfad8fa62b86cc2befe83176a38616c4d42c9e385bfb78e0e838e01。

## 上线记录（2026-09-10 18:39:48 CST）

运行提交`52ebaaa0175f1694545e6b088c77371a4bb8732e`已推送GitHub，CPU fetch准确提交并核验FETCH_HEAD后归档。--check通过，正式部署4文件通过，feature guard 5功能/22条规则通过。备份`/mnt/data-disk/deploy/youtube-auto-publish/backups/flicker-20260910-183948-52ebaaa0175f`。

公网HTML/JS均HTTP200，内容与GitHub blob逐字节一致，版本20260910-flicker-v1；匿名API401。HTML SHA256为72a01f2e392362ab1ecca1b4074b212be3e1c97018be5e40bc922ef99d9a332d，JS为3f9c6b8c5cffee3213ea8fb4f586289683b9759907f83ec05571c19170932142。没有重启服务或修改数据库/SQL。

18:41:18只读回读：API、旧/新worker、统一writer、Nginx五服务active，PID/启动时间/NRestarts=0与18:30基线完全相同。SQL SHA不变，账本6/短链7不变；准备状态generating1、uploading1为现有用户工作。对比报告output/flicker-deployment/postdeployment-comparison-20260910.json。AI后台技能上下文已更新本次故障模式和回滚入口。

本轮无法通过桌面浏览器连接器连接用户现有登录标签（request-header policy加载失败）；不把本机mock浏览器结果标为线上登录浏览器实测。线上证据为实际公网代码字节/版本、服务状态和匿名路由，本机已对同一代码完整复现并验证。

回滚只恢复这4个静态文件，不还原数据库、不重启服务；任何后续文件摘要漂移均拒绝覆盖：

```bash
python3 /mnt/data-disk/deploy/youtube-auto-publish/releases/52ebaaa0175f1694545e6b088c77371a4bb8732e/scripts/deploy_youtube_flicker_fix.py --rollback /mnt/data-disk/deploy/youtube-auto-publish/backups/flicker-20260910-183948-52ebaaa0175f
```
