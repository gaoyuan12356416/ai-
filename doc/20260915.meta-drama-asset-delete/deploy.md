# 部署记录与回滚

## 范围
主机43.166.187.96，服务/root/drama_material_service，systemd drama-material-api.service；公共静态/usr/share/nginx/html。代码来自GitHub gaoyuan12356416/ai- 的 codex/meta-drama-asset-delete-20260915 分支。

## 约束
只覆盖本需求文件。切换前逐文件验证真实生产基线SHA，出现漂移立即停止；备份现有代码、双份静态和台账。备份、release、SQLite均位于UUID 3e8ac4e8-7770-456d-9e89-2ec5dd405fa8的数据盘。

## 流程
1. 本地测试/JS语法/编译/diff检查；GitHub提交并验证远端SHA。
2. 服务端从GitHub获取精确提交；数据盘release里执行Python3.9测试与compile。
3. 检查主API及活跃发布任务，保存unit/timer状态；online SQLite backup，备份自校验manifest。
4. 原子替换限定文件，双份静态使用同版本；仅重启主API。
5. 检查服务active、/api/ui/topbar、页面title/资源hash、未登录接口401；使用只读内部校验确认产品目录和ID解析。不会自动提交DELETE。

## 台账
FB_AD_ASSET_DELETE_DB_PATH默认/mnt/data-disk/fb-ad-asset-delete/tasks.sqlite3；FB_AD_ASSET_DELETE_GRAPH_VERSION默认v25.0。不可回退根盘；业务MySQL必须63350且@@read_only=1。旧台账原位置只读展示。

## 回滚原则
仅回滚代码/静态，保留当前V2台账与未知锁；不要把历史台账备份覆盖当前进度。回滚若恢复旧功能实现，应先关闭本模块入口及旧删除执行路由。精确备份路径、提交、检查结果与回滚命令在部署完成后补录。
