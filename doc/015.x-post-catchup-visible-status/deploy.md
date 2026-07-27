# 部署文档

## 变更内容

- 状态列移动到第 2 列；页面 exact location 禁止缓存；导航入口版本化。
- 新增补发子批次存储、内部 API 和手工 runner。

## 配置项

复用现有两份 X daily/sidecar 环境；不新增 Token，不改变 timer。生产配置账号必须仍为 `2,3,4,5,6,7,8,9,10`。

安装 `deploy/x-post-catchup.service`，但不得 enable，也不得创建 Timer。
以 root:root `0400` 创建 `/etc/x-post-catchup.env`，只写本次获批的
`2026-07-27 / 6 / scope_expansion_v1` 三个非凭证参数。该 unit 复用
daily 的两个 EnvironmentFile、用户、同一 flock、只读 release、数据盘
工作目录和 360 分钟超时。

## 数据库变更

启动 Sidecar 时幂等创建 `x_post_catchup_run`、队列新列/索引/触发器。部署前在在线备份副本上演练并通过 `PRAGMA integrity_check`。

## 部署步骤

1. 完成本地测试、提交并推送精确 Git commit。
2. 记录 live 文件/hash、在线 SQLite 备份、Token hash/mode、env/unit、Nginx/静态双副本和当前 release。
3. 从精确 commit 构建不可变 release并校验 Git blob。
4. 在备份副本演练迁移；原父批次行保持不变。
5. 安装 Sidecar/API/静态/Nginx；`nginx -t` 后窄重启 Sidecar/API、reload Nginx。
6. 真实浏览器验证第 2 列与 9/1 状态。
7. 以显式参数运行一次补发；不得启动 daily service。

## 验证步骤

- 原父批次仍为 3/3 completed。
- 子批次为 6/6 completed，六个预览链接、短链和日志一致。
- 全库素材/账号日/Post ID 无重复，未知结果为 0，SQLite integrity `ok`。
- timer active、daily oneshot inactive、下一次为 2026-07-28 10:00。

## 回滚方案

- 若补发前失败：恢复代码/静态/Nginx；保留数据库审计状态。
- 若任何 X 写入可能发生：禁止恢复或删除数据库历史、队列、日志、素材绑定；只回滚代码并先停止补发。
- 代码回滚到部署前 release，静态/Nginx 从备份恢复；验证 manifest 后窄重启。

## 注意事项

生产补发属于显式一次性操作；runner 不安装 timer。发布后剩余素材必须足以支持下一次 9 账号批次。
