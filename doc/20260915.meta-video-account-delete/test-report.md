# 测试报告

本地Python模块277项通过（原211项、Graph/Source新增25、Store新增26、Service新增11、回滚新增4）。原Page路径的Service集成断言已更新为当前账户User路径；保留旧历史节点读取/诊断测试。

前端真实源码DOM模拟14项通过，Node语法通过；改动Python模块及部署/只读Probe脚本编译通过，git diff --check通过。

GitHub精确提交d4953ad479b2d8259dee5a9c43662918e46537a3已在CPU服务器检出；Python3.9.6执行277项模块测试通过，改动模块及脚本编译通过。

生产GET-only Probe：任务cdb825c5f40f4c86b6fc66c3dffd6d93的17个失败Video/账户对，分布于4个账户，均精确选中源广告投放User631；1次GET /me确认Meta身份122094047877360141，0次DELETE。样本请求act_1029078459940398/advideos、video_id1057811747038229与用户实测参数相同。此检查证明请求路由和Token身份，不证明Meta删除权限。

SQLite升级演练：/mnt/data-disk/meta-ad-asset-delete/schema-rehearsal-20260915T103125Z-d4953ad479b2。14个任务、171个对象、80个尝试、30个成功回执及其他全部旧行保持相等；新增账户进度/尝试表均为空，完整性与外键检查通过。

生产版本于北京时间18:38切换完成：11个运行目标哈希及双份静态一致，公网HTML/JS/CSS和共享topbar为200，未登录的产品/任务接口为401；主API active，原9个发布timer恢复，启动日志无导入/语法/Traceback错误。

原任务所有者会话读取cdb825c5f40f4c86b6fc66c3dffd6d93返回200，17个失败Video及其他阶段历史状态保持。正式备份与线上全部旧行一致，新账户表/账户尝试表均为空；完整性及外键检查通过。对应证据位于recovery-backup-20260915T103803Z-d4953ad479b2中的public-verification.json、authenticated-verification.json和schema-verification.json。

首次prepare因另一个active preview停止，未修改代码/台账；用户明确选择保留参数并重新预览后，先备份、停止原进程，再标记该唯一预览中断。部署后经原所有者会话单次提交相同参数，新预览d2d289fb992f4372bcff7757b4ffb382返回202；8资源、4产品、类型与所有者已逐项核对，未产生执行run。

验收边界：277项后端测试与14项前端测试使用受控数据/Mock；生产Meta仅GET，无真实DELETE。用户报告的样本删除成功是用户实测证据，本次未宣称已代其删除17个Video。
