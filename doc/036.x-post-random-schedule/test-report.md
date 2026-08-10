# 测试报告

状态：通过并已部署。

已通过：随机排期存储、旧库迁移与边界 45 项、两个池 UI 契约 11 项、主后端契约 23 项、X 发布核心 32 项、schedule runner 24 项、Python 编译和 diff check。

浏览器交互已通过：素材池 2 账号 × 3 随机批次显示 6 篇并保存、回显次日计划；短剧池 2 账号 × 4 批次显示 8 篇，缺少 `{{episode_number}}` 时正确拦截，补齐含 `{{url}}` 的合法模板后保存成功。唯一控制台错误为本地 mock 未提供 favicon。

旧库迁移演练和线上迁移均通过；两个服务、两个 timer active，页面 HTTP 200，生产计数和配置未变化。生产实际模板离线渲染均把 `{{url}}` 替换为唯一 `gy.g2flow.com/s2l/<log_id>.html`。恢复 timer 后，自然 claim 为 0、scheduler 为 `no_due`，没有手工或真实测试发帖。

说明：旧 daily/catchup 编排套件在本机 Windows 的媒体本地预检阶段出现与本次排期改动无关的既有失败；变更文件不包含该 runner。Linux 上 `test_x_post_schedule_runner.py` 的测试夹具使用临时 work dir，触发 runner 对固定生产数据盘 work dir 的安全校验；部署后的真实自然 runner 已成功返回 `no_due`。上述既有测试夹具问题未作为本需求的通过证据。
