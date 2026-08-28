# 测试报告（持续更新）

状态：本地回归通过；候选尚未部署生产。以下阶段分别验收，不能互相代替。

## 已完成

- 本地合并 Python 回归：285项中283通过、2跳过，15.333秒。命令：`python -m unittest scripts.test_drama_synthesis_upgrade scripts.test_drama_synthesis_gpu_cache scripts.test_drama_synthesis_cpu_catalog scripts.test_drama_synthesis_remote_client scripts.test_drama_synthesis_gpu_runtime scripts.test_drama_synthesis_media_pipeline -q`。两项跳过是本机未安装COS SDK的真实transport用例，必须在目标SDK环境零skip重跑，不能记为通过。包含 CDN 冻结、上传集成/COS故障、cgroup v1/v2读取；最终候选仍须绑定SHA在Linux重新执行。
- 两页面逻辑/JavaScript：25 项，OK；包括真实函数生成行、总耗时、UTC/北京时间、未知分母不造进度、XSS 转义、10 秒拉取与隐藏/权限门禁。
- 已覆盖超过四小时模拟、丢响应、队列/幂等/冲突、CPU 旧租约、完成原子事务及回滚、通知失败不重制、下载/续传 200/206/416/源变化/半文件、转码集序及兼容直拼、成片检查点、真实短子进程身份。
- 国内/国际 CDN：两组整文件 SHA 相同，三组 Range SHA 相同；性能方向随样本/缓存变化。见 [CDN 实测](cdn-evaluation.md)，不启用统一国际默认。
- 真实应用内浏览器完成组件检查：使用实际页面行函数、样式、运行模块及明确标注的模拟数据，没有模拟登录、API 或生产操作。下载、转码、模板、上传、连接恢复、未知分母六个状态显示正确；DOM 两次读取总耗时从 `4小时0分12秒` 增长为 `4小时1分27秒`。窄窗口按任务表横向滚动，状态文字不逐字竖排。见 [组件截图](evidence/ui-component-browser.png)。此项不等同真实应用认证/API 端到端验收。
- Python 3.9 AST 语法检查通过（18 个本期 Python 文件）；这是语法兼容检查，不等同真实 3.9/3.10 运行时测试。

## 尚待完成

| 验收 | 状态 |
| --- | --- |
| 最新增量的完整回归和候选 SHA 绑定 | 待运行 |
| 真浏览器组件检查 | 通过；真实应用认证/API 端到端仍待验证 |
| CPU Python 3.9 / GPU Python 3.10 Linux 隔离回归 | 待运行 |
| 新异步 API 真实短样故障/重启验证 | 待运行 |
| 下载 1/2/4/8 严格门禁与 CPU 参数对照 | 待运行 |
| 约 90 分钟真实媒体、完整解码与资源曲线 | 待运行 |
| 原任务结果回填核对 | 原正式任务仍在渲染，未操作 |
| GitHub 精确版生产发布、回滚证据 | 未发布 |
| 用户视觉与业务验收 | 未提交人工验收 |

当前未新增服务器、未降低质量、未改生产并发/CPU 配额或其他平台发布行为；没有真实社交平台测试发布。
