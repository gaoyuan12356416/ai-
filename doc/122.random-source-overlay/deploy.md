# 部署与回滚

## 发布边界

仅三条随机模板服务及 CPU 配方生成/UI。GitHub 精确提交先推送，服务器从 origin 取回对应提交；独立 release 目录测试后再切换 current。保持素材目录与编码 profile。

CPU 当前目录不是 Git 仓库，必须从独立 GitHub checkout 取 allowlist 文件；目标仅 features/drama_synthesis/core.py、static/index.html 和 nginx index.html。备份前比较已采集 SHA，禁止整目录覆盖。

## 切换

1. 新发布目录预检及离线渲染成功。
2. 保存 CPU/GPU 服务、指针、代码 SHA、定时器状态和配置备份。
3. TT maintenance gate/pause；暂停 FB 新领取；现有 GPU 请求自然完成，确认空闲。
4. 切换三路 GPU 指针和 Drama release SHA，启动并验证能力、保留的 profile/catalog、所有隧道。
5. 原子更新 CPU allowlist，重启 API/worker，检查生成配方和公共 UI。
6. 恢复本次暂停的领取和原定时器状态；留下健康与最终读取证据。

## 回滚

恢复本次记录的旧指针/系统服务 drop-in 与 CPU allowlist 备份；只重启受影响服务及隧道。回滚前停新领取并排空。新增冻结配方不得交给不支持新字段的旧 renderer：保留已验证新 GPU renderer 完成既有新配方，先回退新配方生成/UI，必要时维持暂停等待修复。不得回滚业务数据库、历史 manifest、源收据或发布账本。

执行时间、提交、备份和验证结果在 test-report.md 追加。
