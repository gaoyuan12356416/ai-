# 测试报告

## 当前结论

隔离验收通过；2026-10-21 19:53 北京时间已上线 CPU 并完成只读核验。本次验收没有调用真实 Graph 写入接口。测试采用临时 SQLite、模拟 Graph/GPU/来源，以及静态页面拦截 API fixture。

## 已执行的后端检查

| 测试 | 通过 | 跳过 | 说明 |
| --- | ---: | ---: | --- |
| test_fb_excel_publish.py | 26 | 0 | 8 列解析、默认值、混合加工/逐行预约、重复确认、签名/身份/配额、原子提交和原操作恢复 |
| test_fb_excel_credentials.py | 13 | 0 | 内部用户映射、个人凭证执行及对账隔离、默认轮换、独立 title、receipt/unknown 停止重发 |
| test_fb_pool_ids.py | 12 | 0 | 分批来源、逐项资格、语言、占用、重复、保序和并发版本 |
| test_fb_excel_review.py | 13 | 0 | 独立逆序时间/同刻顺序、四线程相同操作、入队故障回滚、未知阻塞、全列扫描及稀疏样式预算 |
| test_fb_video_publish_core.py | 49 | 0 | 原发布状态机、回执/未知、锁和旧协议 |
| test_fb_video_page_pool.py | 44 | 0 | 原池计划、排序、暂停和入池签名 |
| tests.test_fb_video_source_links | 37 | 0 | 原来源资格与短链 |
| test_fb_video_publish_broker.py | 18 | 0 | 固定只读 SQL broker 边界 |
| test_fb_auto_repositories.py | 11 | 0 | 旧凭证与来源协议 |
| test_fb_auto_publisher.py | 14 | 0 | 共享 Graph transport 兼容 |
| test_fb_video_publish_gateway.py | 12 | 0 | 网关可信身份、Cookie、模块权限及敏感字段 |
| test_fb_video_pool_identity.py | 18 | 1 | 缺少可选本地线上快照的用例跳过，其余身份及执行校验通过 |
| test_fb_auto_service.py | 2 | 0 | 原共享服务兼容 |
| test_fb_excel_deploy.py | 24 | 0 | 41 目标基线、源/备份漂移、首次启动及外部并发启动边界、功能回滚、恢复失败关闭入口、gate 读回及原状态恢复 |
| test_fb_page_list_performance_deploy.py | 9 | 0 | 既有共享部署守卫 |
| test_fb_page_material_pool_deploy.py | 9 | 0 | 既有池部署与保留账本守卫 |

上述共 311 项通过，1 项可选快照用例跳过。部署测试中出现的部署/回滚 JSON 来自临时目录和模拟 systemctl，不是线上发布证据。

## 浏览器与模板

- Excel fixture 30 项，素材 ID fixture 42 项；旧 Page 加载 33、池计划 47、池读取防重 29、原发布页 80 项，共 261 项检查通过。素材 ID 新增长无效输入保留回归。
- 素材搜索 Node fixture 通过；变更 JS 语法检查、Python 编译与 `git diff --check` 通过。
- 1440px/390px 的 Excel 预检及素材 ID 部分成功界面已截图并检查。
- 新版 `static/fb-excel-template.xlsx` 由 artifact-tool 生成并渲染检查，openpyxl 只读验证 8 列表头顺序、Worksheet、C/D 枚举下拉、B/E 文本格式、无实际数据行/公式。原附件保持原样。

## 缺陷与验收限制

独立审查复现并修复 J 列以后的额外数据/公式被忽略问题；回归覆盖全部列且限制扫描预算。详情见 `bugs/BUG-001.md` 和 `sa-code-review.md`。个人凭证预检核验数据库授权归属及可用状态，平台撤权的最终判断由执行时真实响应完成；失败不得换用其他操作人的凭证。

真实 Meta 上传和 GPU 制作没有用于本次验收。既有发布路径的逻辑由隔离测试覆盖，平台最终发布结果仍以执行回执与对账为准。

## 真实上线结果（2026-10-21）

- GitHub 实现提交 `3e35da5adcc9eda2bdad614a15b2e219042aee7e`；实际部署版本 `a192ce867a1e63d2161f3ca822bfca4a4590de6a`，补充异常 schema 版本的回滚保护。PR：<https://github.com/gaoyuan12356416/ai-/pull/15>。
- CPU 从 GitHub 检出到 `/mnt/data-disk/fb-video-publish/releases/a192ce8`。CPU 上运行 Excel/个人授权/独立审查/部署 76 项及素材 ID 12 项共 88 项通过；最后的部署保护补丁再次运行部署 24 项通过。
- 19:31 首次排空等待达到 120 秒上限，manifest 为 `failed_before_install`，未安装或启动新代码，恢复原状态。既有 GPU 长视频加工继续运行。等待本轮准备服务完成后，19:53:10 开始成功部署，19:53:12 返回 `deployed: true`。
- 41 个目标全部安装并逐一比对 SHA256；7 个公网静态资源均 HTTP 200 且与发布版本一致。下载模板、Page 管理和两份发布页面的缓存版本已同步。`FB_VIDEO_EXCEL_ENABLED=1` 已从实际 sidecar 进程环境读回。
- 仅主 API `drama-material-api.service` 与 FB sidecar `fb-auto-post-service.service` 完成排空后重启；manual worker、池 scheduler 和原 9 个 timer 恢复原 active/enabled 状态。两个重启服务均 active/running、NRestarts=0；启动后日志无 Traceback、fatal 或启动失败。
- SQLite `quick_check=ok`。部署备份的 11 张模块表共有 166 行；原 43 条任务的冻结事实及原 43 条短链身份/URL 保持一致，43 份已生成短链文件与账本匹配。原业务队列正常推进，因此全表状态摘要发生变化，不能声称全部表数据完全未变。
- 使用实际安装模块、重新校验的可信身份及真实只读来源，在空白隔离 SQLite 中复制一个 Page 配置预检：3 行中 1 行有效、1 行素材不存在、1 行 Page 未配置，标题保留，预检前后表数据无变化。真实素材批量解析为 1 有效、1 无效；个人授权映射查询正常返回抽样身份无该 Page 授权。进一步以该身份选择个人 Token 预检，返回 0 有效、1 跳过及 `operator_authorization_unavailable`，未回退为默认 Token，隔离任务/短链仍为 0。
- 验收创建的生产任务、生产短链、真实 Graph 写入均为 0。未通过真实平台发布来测试新功能，也未更改 GPU 服务。

备份 manifest：`/mnt/data-disk/fb-video-publish/backups/excel-20261021T115310.407814Z-a192ce86/manifest.json`；同目录 `operational.sqlite3` 为上线前 SQLite 备份。只读线上证据：`/mnt/data-disk/fb-video-publish/evidence/excel-publish-20261021.json`（权限 0600）。精确功能回滚命令见 `deploy.md`。
