# 香港 Page V2 隔离验收

在香港运行与正式版本相同的 HTTP 入口、SQL 预算、授权检查和画像选择器。QA 使用独立 Token 和回执目录。有消息时只在 QA 本地记录事件。与业务同库的具名 QA 表可实测真实查询与真实写入共用 30 路，SQLite 模式可供独立对照。所有输出都区分调用方到返回 Page 的耗时与后续审计清空时间。

### 香港同库并发真写

使用单次专用目录和表。以下命令使用生产审计表的结构新建 QA 副本。默认拒绝认领已有表。QA 表内有一个携带运行 UUID 的归属行，统计自动排除此行。只有该 QA 表接受回放数据。

~~~sh
python3 ops/fb-page-v2/qa_server.py serve --root /data/fb-page-v2-qa-mysql-1126 --bind 10.89.0.125 --port 8945 --mysql-table fb_page_assignment_qa_20261126
~~~

在 .17 分别向两条回放命令加入 `--expect-mysql-table fb_page_assignment_qa_20261126`。两轮使用不同的 QA 报告文件。QA Token 由上述新目录单独提供。状态页返回数据库 server_uuid、结构摘要、运行 UUID、远端请求头、决策及快照行和本地待确认详情。审计在同一个共享预算中用写角色提交并回读，sql_during_audit 记录审计占用写通道时实际观测的读通道数、前台活动及总占用。读回表统计也使用该预算，按需采样，末尾作独立核对。若需两轮互相重叠，使用独立目录、端口和带不同合法后缀的表，便于逐轮核对。

两轮证据落盘并且待补写归零后，先停止本次 QA 服务。按实际报告的 instance UUID 回收该表并保存回执。本地回执继续保留。工具核对本地身份、数据库实例、表结构、归属行及运行锁。

~~~sh
python3 ops/fb-page-v2/qa_server.py drop-qa-table --root /data/fb-page-v2-qa-mysql-1126 --mysql-table fb_page_assignment_qa_20261126 --instance <报告的运行UUID>
~~~

### 准备香港和发布端

在香港使用正式服务相同的 Python 和 SQL 环境。先就绪只读画像目录与共享 SQL 预算。以下命令从当前 release 的仓库根目录运行，示例中的 Python 需有该服务的依赖。

~~~sh
umask 077
python3 ops/fb-page-v2/catalog_replay_inputs.py --window burst --out /data/qa-input-burst.json
python3 ops/fb-page-v2/catalog_replay_inputs.py --window hour --out /data/qa-input-hour.json
python3 ops/fb-page-v2/qa_server.py serve --root /data/fb-page-v2-qa-run --bind 10.89.0.125 --port 8945
~~~

QA 实例生成权限为 0600 的 qa-bearer。用受控内网通道向真实发布机 10.89.0.17 提供该文件、样本、同版本 qa_replay.py 和 qa_trace.json。保留 8945 的 /32 内网来源限制。也可仅绑定香港 127.0.0.1 取得本机对照。若按上线安排临时复用 8944，由发布负责人确认该端口当前归属 QA。客户端会先向同端口请求受鉴权的 /qa/status，得到独立目标标记后才会发送负载。

### 两个历史窗口

在发布端执行。样本文件使用当前源任务授权的真实剧目和 Page 池，须移交有访问权限的执行机。

~~~sh
umask 077
python3 qa_replay.py --origin http://10.89.0.125:8945 --token-file qa-bearer --inputs qa-input-burst.json --profile burst --output burst.json
python3 qa_replay.py --origin http://10.89.0.125:8945 --token-file qa-bearer --inputs qa-input-hour.json --profile hour --allow-representative --output hour.json
~~~

- 10 秒窗口从北京时间 2026-11-21 15:40:56 开始，为 720 个独立原生请求、1,600 项。工具逐秒核对实际子批长度、产品和任务类型。历史统计未指定池时使用当前原任务已授权的池。
- 忙时从北京时间 2026-11-23 12:39:32 开始，3,600 秒内 6,165 个请求、11,269 项。默认按真实间隔运行。若指定 `--speed 12`，目标时长为 300 秒，报告记录这一倍率。历史精度为秒，同秒的请求一起发出。
- 忙时可复用同一产品真实任务的授权剧目，优先选择历史池、任务类型及准确批次长度。报告给出改用其他授权池、任务类型及取真实剧目子集的请求数。单个样本必须有足够的真实剧目，例如 HotDrama 的 57 项样本。输入不足须补充真实授权样本。执行证据中有单独的源任务去重数。
- 新的回放使用独立幂等键。报告给出完整响应率、有序条目数、不同回执数、Page 命中及未命中原因、P50/P95/P99/最慢响应、客户端延迟派发、SQL 占用、每秒积压、原文清理和回读完成数。耗时从计划到达算到调用方读取完整响应，单请求超时 50 秒。正向选页覆盖与吞吐验收分别可见。
- 结束时三个计数应相等，即独立回执、已回读请求、审计请求头数。审计决策行数需等于提交剧目项数，本地未清理详情、未清理候选快照和积压应为零。所有用同一 QA 目录执行的窗口依次运行，让统计增量与该窗口一一对应。

### 第 31 路与 MySQL 真写验收

可在香港共享预算有 30 个空闲名额时执行既有探针。28 路读和 2 路写角色用 SELECT 核实实例、连接和事务属性。额外请求在中央预算排队，探针只放行自己持有的一路并记录第 31 个请求取得连接的结果。核对输出中的 overflow_queued、states、roles 及 after，无业务数据写入。

~~~sh
python3 ops/hk-sql-budget/verify.py --saturation
python3 ops/fb-page-v2/qa_server.py export --root /data/fb-page-v2-qa-run --output /data/qa-export.sqlite3
~~~

导出使用 SQLite 在线备份接口，完成后输出 SHA-256。将导出及摘要安全移交 CPU。在 CPU 启动独立、无网络且不挂载业务数据目录的临时 MySQL 5.7.44 容器，然后执行同版本审计代码。

~~~sh
docker run -d --name fb-page-v2-qa-audit --network none --tmpfs /var/lib/mysql:rw,size=1536m -e MYSQL_ALLOW_EMPTY_PASSWORD=1 mysql:5.7.44
docker exec fb-page-v2-qa-audit mysqladmin --protocol=socket ping
python3 ops/fb-page-v2/qa_mysql.py --container fb-page-v2-qa-audit --source /data/qa-export.sqlite3 --sha256 <香港导出的SHA256> --workers 2 --output /data/qa-mysql-evidence.json
~~~

等待容器的正式 MySQL 进程就绪再执行。工具只认未接入网络的指定 QA 容器，连接其私有 Unix socket 并核对容器主机名。它使用既有生产表结构和同一写入、提交与主键/哈希回读代码，输出请求头、决策和共享快照数量、实际 MySQL 版本、核实批次数及耗时。相同归档重复核实会先查原记录。容器输出中的 database_rows 与归档总行数应完全一致。证据验收后可移除本次命名的临时容器，香港和 CPU 的 QA 结果供复核。

如需重建负载轨迹，可将原容量评估导出的 demand.json 交给 qa_make_trace.py。随 release 提供的 qa_trace.json 只含到达秒、批大小、产品、池和原生或跨模板类型。
