# 验收记录

## 完成结果（2026-10-19）

- 本地与CPU Linux均31项自动测试通过（包括5项UI测试），Python3.9 AST通过，systemd unit校验通过。磁盘preflight补充5项mock守卫检查通过。
- CPU运行提交9fa54e4bae35aaac1d0b23cb55cb7dee6e601285，GitHub已push并由服务器fetch精确部署。
- 首次报告2026-10-18，趋势2026-10-12至18；17:29:39开始采集，17:31:56发布完成，明确标记首次快照。
- 88项真实数据守恒通过，19个Gate读取会话；来源确认5005条正式图片，其中截至18日4257条；150条生成结果未确认正式入库，单列不并入统计；近7日有指标但无来源的素材0条。
- 18日新增15张，5个剧目，补传0；有消耗素材25张，消耗556.14美元，AF D0收入302.69美元，ROAS54.4269428561%。
- 独立直连只读副本抽查7334488（142.19/79.88）、7345444（134.77/56.39）、7345452（88.79/27.99），消耗/AF D0收入均与日报一致。
- YouTube730张正式素材全部入库日为19日；18日报中0张，确认不会错误归入旧日。
- 固定首页及历史页公网HTTP200，Content-Type text/html; charset=utf-8，Cache-Control no-cache, max-age=0, must-revalidate；3165724字节，SHA256 d8c6920a3b6675f036ef12896444e513312bb35897b0e380f07eae488362205c，两页与本地文件一致。
- 再次运行service返回date_already_published，Result=success/ExecMainStatus=0；没有再次查询或上传。
- ai-image-daily.timer enabled/active，下一次2026-10-20 09:00:00 CST；另外09:10、09:30仅在该日尚未成功时重新尝试。
- 原YouTube worker PID3432663、unified writer PID2937249均active且未变，封面同步timer仍active。

## 浏览器验收

Playwright真实COS页面标题/汇总/趋势/分组/历史页面可用。素材7334488搜索仅一行，消耗142.19、AF D0收入79.88；CSV真实下载一行，UTF-8 BOM及全部指标列。历史页“仅昨日入库”筛出15条。390px手机document宽度390，无页面横向溢出，桌面/手机截图留档。单元UI测试覆盖分页、筛选、排序、切换和注入防护。

非阻塞浏览器信息：COS历史图片URL为HTTP，浏览器自动升级HTTPS并正常显示；桶根favicon.ico返回404，不影响数据或交互。没有业务脚本异常。

## 证据

服务器/mnt/data-disk/ai-image-daily/acceptance.json、status.json、history.json以及runs/20261019T172939310979/下report.json、facts.json、lineage.json、validation.json、publish-receipt.json、index.html。

本地D:/reports/ai-image-daily-20261019/保存验收JSON、公开页面和桌面/手机截图、筛选CSV。运行代码与最终文档提交分开记录，文档更新不意味着运行版本切换。
