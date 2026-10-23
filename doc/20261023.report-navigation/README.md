# 数据报表入口整合

“社媒数据报表”改为“数据报表”，“发布效果分析”改为“社媒效果分析”。新增 `/ai-image-daily.html`，使用共享 QuickNav、UiTopbar 和 `/api/ui/topbar`，沿用 `social_analytics` 模块权限；该权限已可在用户管理中分配，无新增授权或账号变更。

日报以独立 COS iframe 展示，默认固定入口 `codex-artifacts/html/ai-image-daily/index.html`，保留日报原有产品、日期、历史切换、明细、缩略图与 CSV。现有公开 COS 报表的访问属性不改变；后台入口的登录提示和模块权限仅控制后台入口展示，不声称将公开 COS 数据改为私有。不会重新生成、覆盖或补发历史日报，不改发布队列或报表定时器。

## 发布与回滚

基线 `7d780c0` 是本次涉及的线上静态资源快照。仅部署五个静态文件和两份 navigation.json 的定向变更，不整体部署本分支的旧 app.py，不重启任何服务。主运行目录和 Nginx 文档根中的 navigation.json 原本不同，安装器保留各自其他分组、条目、开关和权限，仅调整报表分组。

先推送 GitHub，再在 CPU 的已存在仓库 fetch 精确 SHA，使用该 SHA 的 `ops/report-navigation/deploy.py --repo <repo> --commit <40位SHA>`。安装器验证数据盘 UUID/容量、所有线上文件预期 SHA，保存字节备份与清单，原子替换每个文件，读取 SHA 验证。

回滚：`python3 <本次安装器路径> --rollback <部署输出的backup目录>`。回滚会检查当前文件和备份未漂移，恢复原字节；新加的两个页面资源删除。保留所有业务数据库、素材、日报文件和发布/投递记录。

## 检查

- `node --check`：quick-nav.js、social-analytics.js、ai-image-daily.js。
- `node ops/report-navigation/check-ui.cjs`：未登录、无权限、已授权、管理员、退出时移除 iframe；旧导航迁移保留禁用状态。
- `python -m py_compile`：技能要求的核心文件及部署器；`git diff --check`。
- main/public 两份真实导航定向修改幂等，其他分组保持相等。
- 公网回读精确文件内容、实际登录浏览器查看导航和日报，并检查历史切换；记录 API 状态和日报代码指针，本部署不执行服务重启。

## 维护约束

入口保持指向固定首页，不写死某一天。新增日报业务计算仍由独立 `ops/ai-image-daily` 负责；不要为导航改名重新部署该服务。权限 key 和已有用户授权保持 `social_analytics`。

## 生产验收

- 静态运行提交：`2d97d17b82ce2d3970f4965b75bf7969de2d0b95`，GitHub push 与 CPU fetch 均已验证。
- CPU：`43.166.187.96`，主副本 `/root/drama_material_service/static`，公网副本 `/usr/share/nginx/html`。
- 备份：`/mnt/data-disk/report-navigation-20261023/backups/20261023T032257Z`；12 个目标全部回读成功，公网五个文件与 Git blob 字节完全一致。
- 安装器：`/mnt/data-disk/report-navigation-20261023/releases/2d97d17b82ce2d3970f4965b75bf7969de2d0b95/ops/report-navigation/deploy.py`。
- 精确回滚：`python3 /mnt/data-disk/report-navigation-20261023/releases/2d97d17b82ce2d3970f4965b75bf7969de2d0b95/ops/report-navigation/deploy.py --rollback /mnt/data-disk/report-navigation-20261023/backups/20261023T032257Z`。
- 本次未重启/reload 服务。主 API 当前 active、PID 826226，启动时间 11:19:32 CST 早于本次 11:22:57 静态安装；与最初观察 PID 不同，不能声称整个操作期间 PID 恒定。日报 timer active，代码仍为 `60c4e7c02da1db0ecfddb4feea31ce8a162ed0c2`。
- 登录浏览器实际打开新入口，导航两处更名及当前项展开正确。嵌入日报显示 2026-10-22，生成时间 2026-10-23 09:00:01 +08，数据校验通过；三产品汇总为新增正式素材 67、累计 5,811，证明实际报告脚本和数据已载入。首次 COS 资源加载较慢，页面显示加载提示后正常完成。
- 技能参考 `ai-backend-maintenance/references/ai-image-daily.md` 已更新入口与维护边界。
- 实测历史目录含 10 月 22 日和 21 日，切到 21 日后保留后台外框，显示历史快照提示及新增 60；旧社媒页刷新一次后标题显示“社媒效果分析”。已打开的旧页面可能保留浏览器缓存，刷新即可显示新名称。
