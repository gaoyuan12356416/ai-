# 生产上线验收 — 2026-09-24

## 已上线

- 页面：https://ai.yingliangads.com/youtube-analytics.html ，入口 YouTube 社媒 > YouTube 数据报表。
- 分支：codex/youtube-analytics-report-20260924，GitHub推送已验证。
- 功能运行提交：f338244ad3a078eb200856762451fa66527f694b。
- Nginx代理安装器提交：f1c900b3005bc10a50ddb43bf609e4202a60f2b7（包含最终配置及重载有界读取校验）。
- CPU：43.166.187.96，主API /root/drama_material_service，静态 /usr/share/nginx/html；代理 /etc/nginx/default.d/youtube-analytics.conf。
- 精确GitHub release目录均为 /mnt/data-disk/deploy/youtube-auto-publish/releases/<commit>，服务器fetch/checkout已核验。
- 功能备份：/mnt/data-disk/deploy/youtube-auto-publish/backups/analytics-20260924-114041-f338244ad3a0。
- 代理备份：/mnt/data-disk/deploy/youtube-auto-publish/backups/analytics-proxy-20260924-114519-f1c900b3005b。

## 测试和真实数据

- Windows及Linux Python3.9各40项后端、3项部署/回滚测试全通过；浏览器mock 28项通过，无runtime error，桌面/手机布局截图已检查。Nginx重载404→401过渡额外mock回归通过。
- GitHub release部署前 --check通过，14个代码/静态/导航目标均符合manifest hash；公网HTML/JS/CSS/quick-nav/navigation 5个文件HTTP200且字节hash匹配。
- 现有管理员和普通用户Cookie仅在服务器进程内使用，没有打印/归档凭证。loopback及公网options/report/export均200；普通用户只见本人，管理员同租户；非法参数400，公网未登录options/report/export均401。CSV UTF8 BOM及no-store已验证。
- 2026-09-17至09-23 UTC只读真实查询：446条日期+归因组合，239个唯一可归属链接键。全站源表4109点击/6164访问/625安装/70充值人数/$964.39收入；可归属4101点击/6144访问/623安装/70充值人数/$964.39收入；未唯一归属差额8点击/20访问/2安装/$0收入，不分摊。逐指标守恒成立，源表充值人数为每日人数相加而非跨日去重。
- 发布账本237、发布准备230、短链243、手动短链6，上线前后计数一致；全部历史短链身份、URL和wrapper hash投影不变，SQLite quick_check=ok。没有创建测试发布、评论、图片、短链或通知。
- 主API PID从2566075变为3373003，active/NRestarts=0；自动发布worker PID1482318、统一writer PID2937249保持不变且active。API上线后102条日志无ERROR/Traceback标记。
- 主API仅在功能安装时重启；代理安装只nginx -t和平滑reload。无新service/timer、无schema迁移。

## 修复的部署发现

首次公网检查发现新/api/youtube-analytics前缀未代理（loopback正常），补独立代理；第一次代理读回在重载旧worker未退出时返回404，安装器按保护逻辑撤回新增配置。加入最多10次有界读回后GitHub重新发布，11:45:19安装成功并完成公网真实Cookie验收。未保留失败安装配置。

## 完整回滚

先删除本次新增代理并平滑重载，再恢复14个主API/静态/导航目标：

```bash
python3 /mnt/data-disk/deploy/youtube-auto-publish/releases/f1c900b3005bc10a50ddb43bf609e4202a60f2b7/scripts/deploy_youtube_analytics_proxy.py --rollback /mnt/data-disk/deploy/youtube-auto-publish/backups/analytics-proxy-20260924-114519-f1c900b3005b
python3 /mnt/data-disk/deploy/youtube-auto-publish/releases/f338244ad3a078eb200856762451fa66527f694b/scripts/deploy_youtube_analytics.py --rollback /mnt/data-disk/deploy/youtube-auto-publish/backups/analytics-20260924-114041-f338244ad3a0
```

两步均校验后续漂移。仅恢复代码/导航/代理，保留当前SQLite及所有发布、预约、资产、短链和投递事实；禁止将before.sqlite3覆盖当前库。

ai-backend-maintenance技能的youtube-auto-publish参考已追加报表口径/安全边界与回滚指针。未修改memory文件。
