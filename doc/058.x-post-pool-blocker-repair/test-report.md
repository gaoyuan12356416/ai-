# 测试报告

## 本地验证（2026-08-28）

- Windows Python 3.14全X回归：`python -m unittest discover -s scripts -p 'test_x*.py' -q`，**832 tests / 56.942s，OK，2项POSIX用例skip**。
- 聚焦账号隔离187项、媒体/OAuth155项、历史恢复CLI6项均通过。
- 首次全量出现新错误码未登记，已修复并重跑全量；无未解决失败。
- 独立代码评审未发现可证实P1/P2；`git diff --check`通过。
- 运行使用模拟X/GPU、本地HTTP和临时SQLite；没有访问真实X发布接口。
- 完整本地日志：`output/x-pool-blockers-20260828/local-x-tests.log`（工作区输出，不入仓库）。

## 生产只读快照与备份

- 备份目录：`/mnt/data-disk/x-post-automation/backups/20260828-pool-blockers-155312`。
- SQLite online backup：quick_check=ok、foreign_key_check=0。
- Run348原13条+Run350原3条：failed、attempt=0、unknown=0、无媒体/帖子ID、未开始发布，relay源/转发尝试均0。
- q533/719/726完整queue/log/relay快照受保护，后续不得清除或重发。
- 15:47:47外部暂停、15:59:58恢复timer；重新门禁后按当前active状态维护部署，16:13:44恢复原状态。

## 生产代码验证

- 首轮提交814e168在服务器Python3.9.6：832/832，38.593s，无跳过。
- 备份副本初始化完整性ok/FK0，业务表数量不变。
- 16:13:44代码部署检查完成，Sidecar/Main auth/GPU均200，主API X admin未鉴权401；三个timer恢复active。
- 真实内部只读预检：账号8返回x_post_account_locked，账号19返回x_post_account_needs_review，均409，中文原因指向正确历史日志。
- 保护533/719/726的queue/log/relay逐字段不变；此阶段没有历史队列重置或新增Post/Repost。

## 最终核验结论（18:52）

媒体prepare、copy/live validate与apply及原timer自然发布均已完成。Run348为13/13、350为3/3，11条原转发全部完成；新增失败0/unknown0，每个源帖和转发各1次尝试，每个剧集进度仅推进1集。账号8的外部锁定与账号19的既有unknown仍待人工处理，本次没有清除或重试这些保护记录。

## 追加下载完整性与恢复证据验证

- BUG-003真实复现GPU静默截断：声明83,863,962字节但实际6,291,709。CPU完整SHA对应首次失败job，排除原CPU源错误；GPU指纹门禁正确停止，0 X写入。
- 下载补丁170e3b1：CPU Linux836/836（38.904s）；GPU138/138（4.234s）；17:35健康与timer恢复通过。17:40真实GPU单次GET完整83,863,962字节/SHA与CPU一致。
- 新错误码素材重查分类先红后绿：补集合前4个subtest失败，补后183项相关回归通过；root独立116项store回归通过（14.306s）。dad987b最终功能提交Linux836/836（38.730s）通过，17:45上线健康及保护账本检查通过。
- GPU复用helper：原6项CLI测试保持不变，新增12项；合并Daily/GPU100项通过。固定真实bundle与冻结行纯合同门禁7/7，仅代表证据结构可用，不代表CPU实际输出验证或发布。
- 事故wrapper新增8项永久测试，恢复相关26/26通过；加入两份helper后的Windows全X回归856项/62.517s通过（2项POSIX用例skip）。完整日志为工作区output/x-pool-blockers-20260828/local-x-tests-with-recovery.log。
- wrapper独立审查无可证实P1/P2；新factory推进1小时可复用完整item且媒体调用0，16条冻结身份跨时间不变，先恢复348后350自身数据库门禁仍通过。临时SQLite与固定证据的9项离线验证通过。
- 当时（17:55）逐项真实CPU checkpoint、copy/live apply及自然消费尚未执行；最终实测结果见下文。apply使用已持有的原process_lock、两run共用最终index SHA，factory/store/execute_recovery指向同一目标DB。

## 18:15恢复准备进度

- CPU e300542最终Linux856/856（38.484s）通过，17:59恢复工具切换后健康/保护行/三个timer均通过，见recovery-ops-deployment-result.json。
- Run348实际prepare完成13/13且status=validated：7条GPU历史缓存成品重新由CPU下载/探测、6条正常原源修复；X写入0，原队列未改。Run350的3条正在同一路径串行准备。
- 独立审查运维apply命令：发现audit只验数量的P2后已补完整27个非id字段。16条正常audit通过，分别篡改27列均被拒绝；另补stop部分失败、timer恢复/查询异常的持久化，以及进入apply后失败不自动恢复timer的保护。
- 运维脚本以独立operator commit从GitHub取出并从文件执行，保留`<commit>/deploy/recovery/`归档层级；不切换服务current，helper和审计证据仍绑定e300542。

## 18:25副本演练门禁

- 两批实际媒体prepare均完成（348为13/13，350为3/3），最终16条index SHA为a9f056b91faca4557107d590fbe9a74f6fa9d3a50c005d4c1b4d871e1aa07561；没有X写入。
- 第一次copy命令在保存恢复前完整账本快照时被证据大小门禁拒绝：原始JSON为2,834,994字节，超过helper既有2MiB上限。两个run未进入validate/apply、恢复audit=0；只读核对完整业务快照与刚创建的副本备份完全一致。
- 仅调整独立operator脚本的账本快照格式为gzip+base64封装，记录原始SHA、字节数和各表行数；既有2MiB门禁、checkpoint、helper、CPU/HK服务版本均不变。真实副本只读计算的封装为533,590字节，解码还原与原始JSON完全一致。
- 第一次失败的命令日志/结果/副本备份需保留为attempt-1证据；确认没有恢复写入后才再次执行copy阶段，不重做16条媒体准备。
- 已归档到attempt-1-e4dd990-evidence-size。压缩小改经独立离线复核：3,205,033字节模拟快照无损还原、SHA/长度/六表数量一致；超过2MiB的压缩封装仍由真实_write_private拒绝且不创建文件。此前16条正常audit、27列/4类篡改及6类timer测试全部保持通过，报告见工作区output/x-pool-blockers-20260828/recovery-apply-offline-review.json。

## 18:34生产恢复与首次自然调度

- 最终operator提交386d4473e72fd9ffc6a935948df24e46fe45c25c；CPU GitHub归档后实际文件SHA=d8b2b9b04e0640250d7940172382830a781e4280a92318d14c3a97b808b1a41a，服务current仍e300542。
- Copy于18:33:08、live于18:34:28验证完成，两个run均先全部validate、再分批apply（13+3）；各自全程持原process_lock，apply时媒体/GPU调用被禁止。
- 每阶段16条原queue/log、11条relay、16条完整不可变audit验收通过；所有其他业务行保持不变，quick_check=ok/FK=0。live三个timer按原active状态恢复，准备/恢复命令X写入0。
- 四份before/after压缩证据已独立解码验SHA、字节数和逐表行数，无损；live前后原始JSON分别2,834,994/2,862,942字节。数据库仍uid987/gid981/0600，未改变权限。
- 18:35:10原schedule timer自然启动，18:36两条已有真实Post ID；18:39成功5条，其中2条relay为reposted，失败0/unknown0。此为真实原队列发布，不是测试帖。
- 证据：copy-recovery-result.json、live-recovery-result.json、operator-compressed-stage-result.json、copy/live-ledger-before/after.json与natural-progress-20260828T103650Z.json，均在原私有备份目录；保护533/719/726逐字段不变。

## 18:52最终自然发布验收

- Run348于18:46:58完成13/13，Run350于18:49:55完成3/3；16个不同源Post ID、11条reposted/5条direct，source/repost每条各1次尝试，新增失败0/unknown0。
- 独立只读SQLite复核：16条queue相对于live恢复后的完整快照仅status/updated_at改变；原material、账号、正文、路由、预检SHA/大小等字段均未变。16条完整恢复audit未变化。
- 16个pool原绑定、assigned_source_queue_id、content和replay_generation未变；next_sub_number和published_episode_count各精确+1，状态与免费集数上限一致，错误均清除。未插队换剧。
- q533/719/726完整queue/log/relay逐字段保持原快照；360仍17成功/1失败/1待确认；356、363仍历史0队列预检失败，没有重建。
- X全局在途写0，SQLite quick_check=ok/FK=0，三个timer与Sidecar/Main API均active；8810/auth/18820均200、X未鉴权401。CPU current=e300542、PID1840134，main和sidecar service SHA仍87c42536dac4a1a9b8eb906c973ef056f7b5dfaaddc7b391d327448184fde7cf。
- 香港独立只读复核current=170e3、worker1780296/tunnel1780297 active/running，活动work目录0，service SHA仍e63a4e04b622b95b0a61489e90984b03317183726ac8420ceb9f5e0e427356e5。未操作US节点或改变/data路径。
- 最终证据：CPU原备份目录final-natural-verification.json；HK `/data/x-post-media-repair/backups/20260828-pool-blockers-download/final-hk-verification.json`。18:52已向迁移协调任务回传精确版本与证据；无需重复发布或修改unknown。
- 新逐条deferred修复分支有离线覆盖；本次16条历史恢复使用完整preflight证明路径并通过真实自然发布，不把它冒充为人为创建新排期或新deferred分支的生产测试。
