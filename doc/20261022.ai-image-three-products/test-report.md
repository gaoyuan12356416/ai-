# 测试与上线记录

数据所属日：2026-10-21；七个完整后台统计日：2026-10-15 至 2026-10-21。金额单位 USD，收入为 AF D0；沿用后台 `dt`，不重新换算广告账号当地自然日。

## 版本与冻结产物

- 运行代码：`60c4e7c02da1db0ecfddb4feea31ce8a162ed0c2`，已从 GitHub 精确拉取并逐项核对 16 个模块文件的 Git blob。
- 冻结批次：`/mnt/data-disk/ai-image-daily-v3/runs/20261022T103659212933`。
- 资源版本：`20261021-7080be0e7235fc18`；123 个文件。
- manifest SHA256：`db09ad76b15ac83d8d5030b2d3c8fbf4429a84fb685ab09be31c228172be6c3d`。
- 安装备份：`/mnt/data-disk/deploy/ai-image-daily/backups/20261022T024858.819262Z`。
- 本地证据：`D:\reports\ai-image-three-products-20261022`。

## 自动检查与独立审计

Windows 与 CPU Linux 均运行 `python -B -m unittest discover -s ops/ai-image-daily -p '*test*.py' -q`，115 项通过：55 个旧账本用例、32 个三产品用例、20 个发布/缩略图用例、8 个 UI 用例。Python 3.9 AST、JavaScript 语法与 diff 检查通过。

覆盖迁移去重、跨产品来源与投放归属、同名/同 ID 剧目、历史删除正式素材、新旧正式 ID 同时有消耗、真实零消耗、数据读取失败、未知来源、匹配其他图片对照、gzip/缓存、冻结篡改、多对象未知结果恢复、回滚健康状态和缩略图边界。自动测试无生产写入。

生产报告自身 921 项守恒检查通过；另由独立 Decimal 审计完成 584 项检查，没有调用报告的分析计算函数。未知有消耗素材为 0。应用清单 DramaWave 148 个、MoboShort 17 个、MoboReels 4 个，包含各 root 和各自 W2A，排除 DramaWave Minis 3346。来源未确认记录 156 条保留在待核对清单，没有纳入正式素材数。

| 产品 | 昨日新增正式素材 | 累计正式素材 | 昨日消耗 | AF D0 收入 | ROAS |
| --- | ---: | ---: | ---: | ---: | ---: |
| DramaWave | 36 | 5,552 | 352.69 | 224.77 | 63.73% |
| MoboShort | 12 | 90 | 568.39 | 255.00 | 44.86% |
| MoboReels | 12 | 102 | 101.46 | 0.32 | 0.32% |
| 合计（正式 ID 去重） | 60 | 5,744 | 1,022.54 | 480.09 | 46.95% |

七日累计消耗 4,827.85，AF D0 收入 2,168.17，ROAS 44.91%，有消耗正式素材 354。同期新增正式素材 1,532、首次原图入库 975、迁移 557、历史补传 730；首次原图与历史补传是不同维度，不将它们直接相加。

旧 DramaWave 同范围的正式 ID 集合完全相同，都是 5,552 个；近七日 654 个原始分组键无新增/删除。10/21 既有 33 组上游数据从约 09:00–09:02 更新至约 10:35–10:40，消耗增加 1.96、收入增加 0.20、曝光增加 279、点击增加 19、安装增加 4、AF 安装增加 3、付费不变。这是投放及归因数据回补，没有发现计算或范围差异。逐行证据见 `independent-audit.json`。

## 真实数据浏览器验收

使用冻结服务端 `server-report.json`，桌面及 390px 手机宽度完成检查：三产品和合计的日/周数据、日期导航、5,744 条素材明细、六个分组的金额、分页、真实 ID 搜索和 CSV 下载解析均通过。

真实素材 7345452 的周消耗/收入为 465.74/224.22，昨日为 61.32/27.16，切换及导出吻合；手机 MoboReels 素材 7682513 显示 31.11/0.00。手机 scrollWidth=390，无整页横向溢出。首屏没有请求原图或明细。截图、CSV、自动化证据保存在 `browser/real-browser-evidence.json` 旁。

## 性能基线与发布验证

旧固定首页 HTML 为 5,222,219 字节，无 gzip；本机诊断下载用时 15.423 秒（首字节 0.526 秒）。用户引用的 10/18 历史页为 3,165,724 字节，下载用时 10.247 秒。历史链接是冻结快照，不会变成昨日数据。

新版实际冻结 HTML gzip 24,113 字节、JS gzip 12,439 字节，必要首屏合计 36,552 字节（35.7 KiB，低于 150 KiB）。明细按产品延迟加载。130 个素材使用 114 张去重缓存缩略图，全部生成成功，耗时 48.217 秒；首屏不请求图片。

### 公网实测结果

2026-10-22 10:54 CST，在本次诊断同一台本地电脑、同一网络测量；每次使用新建隐私浏览上下文，禁用网络缓存并阻止 Service Worker。以四行产品汇总表可见且完成两次动画帧为终点，三次为 **1.0381 / 1.2086 / 0.9207 秒**，中位 **1.0381 秒**，满足 3 秒内目标。三次均没有磁盘/Service Worker 缓存命中，首屏明细和图片请求均为 0。

含非阻塞状态文件正文合计 36,693 字节，含 HTTP 头实际传输 38,173 字节；第一次另有浏览器默认 favicon 404 共 657 字节，不影响日报显示。JavaScript 运行错误为 0。报告没有在线数据库接口。

同样使用本地 curl 下载新首页，三次 **0.661447 / 0.901642 / 0.930821 秒**，中位 0.902 秒；旧首页诊断下载 15.423 秒。这组是下载对比，不能把旧下载用时直接称为旧浏览器汇总渲染时间。HTML 从 5,222,219 字节降至 gzip 24,113 字节；新版 HTML+JS 35.7 KiB。

公网实际页面还验证了三产品/七日切换、真实素材 CSV 下载、历史日期页保留 `#materials`、旧入口跳转新首页保留 `#performance`、390px 手机展示及真实缓存缩略图。可重复证据为 `browser/public-acceptance-evidence.json`，截图 `public-desktop.png`、`public-mobile.png`、`public-mobile-material.png`。

### 发布与调度结果

2026-10-22 **10:52:49 CST** 发布成功。123 个 manifest 对象及独立 health 均验证公网原始/压缩字节与元数据；pending 已清除。主页面 SHA256 为 `ffa316dc1b2e915fd9f8330fe61fe76171210df544cdbde0364dc59be7a3c591`。

服务器再次独立回读新固定首页、历史页、目录、旧入口，均符合冻结清单。旧 10/18、10/19、10/20、10/21 四份历史页 SHA256 全部未变，旧 state 的 history/status 未变；三个既有后台/发布服务的 PID、启动时间与状态均未变。

同日再次启动 service 返回 `date_already_published`，status 文件逐字节不变，没有生成新 pending。唯一 `ai-image-daily.timer` 已恢复为 active/waiting；下次 **2026-10-23 09:00 CST**，保留 09:10、09:30 两次失败补偿。没有创建第二套调度，没有补传素材、广告发布或业务库写入。

部署安装备份见上方，公开可变对象备份为 `/mnt/data-disk/ai-image-daily-v3/backups/20261022T103659212933`。服务器验收记录 `/mnt/data-disk/ai-image-daily-v3/acceptance.json`；本地 `server-acceptance.json`、`publish-receipt.json`、`deployment-rollback.json` 保存对应证据。精确回滚命令见 `deploy.md`，本次未执行回滚。

固定报告：[三产品日报](https://advertising-1306474899.cos.ap-hongkong.myqcloud.com/codex-artifacts/html/ai-image-daily/index.html)。首份快照：[2026-10-21](https://advertising-1306474899.cos.ap-hongkong.myqcloud.com/codex-artifacts/html/ai-image-daily/daily/2026-10-21/index.html)。

