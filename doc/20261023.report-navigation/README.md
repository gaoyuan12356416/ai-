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
- 公网回读精确文件内容、实际登录浏览器查看导航和日报，并检查历史切换；API PID 和日报代码指针保持不变。

## 维护约束

入口保持指向固定首页，不写死某一天。新增日报业务计算仍由独立 `ops/ai-image-daily` 负责；不要为导航改名重新部署该服务。权限 key 和已有用户授权保持 `social_analytics`。
