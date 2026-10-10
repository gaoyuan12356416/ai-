# 测试报告

2026-10-10，北京时间。

- 后端188项测试全通过：新增精确素材、HTTP路由、跨语言Page、事务并发、固定回执、源视频时长探测，以及既有FB自动/手动/授权/容量/发布保护回归。
- 部署回滚故障注入2项通过，覆盖部分复制失败、新模块尚未创建及单文件恢复失败后的服务启动。
- Node交互测试通过：空/多ID、禁用模板、storage异常、重复点击、断网/刷新同操作重试、通用400保留、明确409/503探测失败释放、中文跳过/XSS转义和再发一轮。
- 既有频次UI回归通过；Python编译、JS语法、git diff --check通过。
- Playwright真实浏览器通过：点击按钮、单ID输入、确认模拟任务、展开跳过原因、跳转本轮详情。仅本地fixture，无真实Facebook发布；favicon 404为本地测试环境缺图标。
- 生产fb-auto-post用户可执行/usr/bin/ffprobe。

生产验收完成：CPU服务器140项相关测试通过；服务用户ffprobe可执行。生产数据隔离副本读取145个真实Page，125个可入队、20个按暂停/未知/活动素材占用跳过，重试返回同一run，MySQL @@read_only=1已核验；这些任务只存在隔离副本，不会被生产worker执行。线上拒绝性探针返回400且真实爆款运行数仍为0。

正式代码发布前后业务表哈希未变，侧车服务健康、9个相关timer均active；公网HTML/JS/CSS/运行页均200且与GitHub源字节一致，匿名请求401。

本地完整后端回归命令（188项，37.822秒）：

```powershell
python -m unittest scripts.test_fb_hit_material_publish scripts.test_fb_manual_material_batch scripts.test_fb_auto_service scripts.test_fb_auto_store scripts.test_fb_auto_repositories scripts.test_fb_auto_publisher scripts.test_fb_auto_v2 scripts.test_fb_post_strategy scripts.test_fb_auto_frequency_cutover scripts.test_fb_auto_guarded_disable scripts.test_fb_capacity scripts.test_fb_auto_app_contract -q
```

服务器回归命令（140项，21.882秒）：

```sh
python3 -m unittest scripts.test_fb_hit_material_publish scripts.test_fb_manual_material_batch scripts.test_fb_auto_repositories scripts.test_fb_auto_store scripts.test_fb_auto_v2 scripts.test_fb_auto_publisher scripts.test_deploy_fb_hit_material
```

此外独立SA验证24项通过；两个Node脚本和Python/JS语法通过。技能ai-backend-maintenance已增加该入口的回执/探测/回滚规则；保留技能目录里原有未提交改动。测试未调用真实Meta/GPU发布写接口；排队能力验证与实际平台发布分开。
