# 部署文档

## 最新发布

Google CPC / 图片视频CTR新规则已于2026-08-27北京时间15:22上线，精确提交`72f2e7440e16d8d3ea782ce9eea31176d21c0797`。当前版本、七个月结果、备份及精确回滚程序见[Google CPC实际发布记录](release-google-cpc-20260827.md)。下文V1/V2部署与PENDING交接均为历史，不替代该最新记录。

## 变更内容

- 新增独立公开静态路径 `/reports/opay-excellent-creatives/`。
- 新增 `/opt/opay-excellent-creatives/releases/<commit>` 与 `current`。
- 新增数据盘缓存/快照/缩略图和公开静态目录。
- 新增初版、终版两个月度 timer；不修改旧报表 unit。

## 配置项

生产环境文件 `/etc/opay-excellent-creatives.env`，`0600 root:root`。配置仅保存路径、超时和媒体允许主机等非公开运行参数；MySQL 凭据继续由现有本机只读命令模块提供，禁止进入 GitHub/日志。

## 数据库变更

无 MySQL DDL/DML。所有查询必须在 `101.32.56.53:63350` 验证 `@@read_only=1` 后执行；写端口 63353 不在本需求范围。

## 部署步骤

1. 本地完成编译、单元、前端契约和 `git diff --check`。
2. 提交并推送分支，记录精确 GitHub commit。
3. 在服务器验证 `/mnt/data-disk` 为 UUID `3e8ac4e8-7770-456d-9e89-2ec5dd405fa8` 的已挂载可写文件系统并检查空间。
4. 验证 GitHub SSH，fetch 精确 commit 到新的不可变 release；不得从本地直接复制源代码伪装成 GitHub 发布。
5. 在 `/mnt/data-disk/opay-excellent-creatives/backups/<timestamp>-pre-<sha>` 备份旧 current、公开提交点、Nginx、env 和 units，生成并校验 SHA-256 清单。
6. 安装新 env、Nginx 和 systemd 文件，执行服务器端编译/测试。
7. `nginx -t` 成功后 reload Nginx；先不启用 timer。
8. 使用影子输出完成 2026-07 回归和媒体抽样，再回填 `2026-01` 至最近完整月份。
9. 检查每月快照后原子发布，最后切换 `current` 和 `latest.json`。
10. 启用并启动两个 timer，完成公开、旧系统和只读抽样验收。

## 验证步骤

```bash
python3 -m py_compile /opt/opay-excellent-creatives/current/ops/opay-excellent-creatives/opay_excellent_creatives.py
python3 -m unittest discover -s /opt/opay-excellent-creatives/current/ops/opay-excellent-creatives -p 'test_*.py' -v
python3 /opt/opay-excellent-creatives/current/ops/opay-excellent-creatives/validate_frontend_contract.py
sqlite3 /mnt/data-disk/opay-excellent-creatives/cache/opay-excellent-creatives.sqlite3 'PRAGMA quick_check;'
nginx -t
systemctl status opay-excellent-creatives-initial.timer --no-pager
systemctl status opay-excellent-creatives-final.timer --no-pager
curl -sS -I https://ai.yingliangads.com/reports/opay-excellent-creatives/
curl -sS https://ai.yingliangads.com/reports/opay-excellent-creatives/latest.json
```

还需验证匿名 200/无 Location、robots、2026-01 起月份清单、Google 无估算、Meta/TikTok 非空、素材 ID/产品范围、公开 JSON 不含密码/Token、主 API和旧 AI Game Performance 行为不变。

## 回滚方案

1. 停止并禁用两个新 timer，确认 oneshot service 不在运行。
2. 将 `current` 原子切回备份记录的上一 release；若首次部署则移走新 current，不删除。
3. 恢复备份的 Nginx/env/units/公开 `index.html` 和 `latest.json`，清单最后恢复。
4. `systemctl daemon-reload && nginx -t && systemctl reload nginx`。
5. 保留 SQLite、快照和缩略图用于审计，不在常规代码回滚中恢复或删除数据盘事实。
6. 复核新路径不再提供本报表内容，并检查旧报表、主 API、Nginx 和磁盘；实际 HTTP 状态取决于父级 server 的既有兜底行为。

## 生产发布记录

- 发布时间：2026-08-26（Asia/Shanghai）。
- GitHub 分支：`codex/opay-excellent-creatives-report-20260826`。
- 运行提交：`0cba014b56f1c6394a9d0d3be5d735a370f83659`。
- 当前 release：`/opt/opay-excellent-creatives/releases/0cba014b56f1c6394a9d0d3be5d735a370f83659`；`current` 已解析到该目录。
- 首次部署，无前一 release。上线前备份：`/mnt/data-disk/opay-excellent-creatives/backups/20260826T185200+0800-pre-0cba014`；`manifest.txt` 与 `SHA256SUMS` 校验通过，记录 `pre_current/public/nginx/env/units=absent`。
- 数据版本：`20260826T184515557171+0800`，清单 SHA-256 `7b272cb2ea01e8a1ac9da0361d124ff517022bea0867a29cbf07cf3decb4d0dc`。
- 回填：2026-01 至 2026-07 全部为终版，共 186 行。瞬时单元 `opay-excellent-creatives-backfill-0cba014.service` 成功退出，`ExecMainStatus=0`。
- `opay-excellent-creatives-initial.timer`、`opay-excellent-creatives-final.timer` 均 enabled/active；下一次分别为 2026-09-03、2026-09-05 北京时间 10:00 左右（含 `RandomizedDelaySec`）。
- `nginx -t`、systemd unit verify、匿名 HTTP 200、noindex、版本化 JSON、旧报表 302 行为和主服务回归均通过。

## 首次部署精确回滚命令

本次上线前对应对象均不存在，因此回滚采用“移到保留目录”而不是删除，SQLite、快照、缩略图和不可变 release 均保留：

```bash
set -euo pipefail
opay_rollback_hold=/mnt/data-disk/opay-excellent-creatives/rollback-hold/20260826-first-release
install -d -m 0700 "$opay_rollback_hold"

systemctl disable --now opay-excellent-creatives-initial.timer opay-excellent-creatives-final.timer
test "$(systemctl is-active opay-excellent-creatives-refresh@initial.service || true)" = inactive
test "$(systemctl is-active opay-excellent-creatives-refresh@final.service || true)" = inactive

mv /etc/nginx/default.d/opay-excellent-creatives.conf "$opay_rollback_hold/"
mv /etc/opay-excellent-creatives.env "$opay_rollback_hold/"
mv /etc/systemd/system/opay-excellent-creatives-refresh@.service "$opay_rollback_hold/"
mv /etc/systemd/system/opay-excellent-creatives-initial.timer "$opay_rollback_hold/"
mv /etc/systemd/system/opay-excellent-creatives-final.timer "$opay_rollback_hold/"
mv /opt/opay-excellent-creatives/current "$opay_rollback_hold/current"
mv /usr/share/nginx/html/reports/opay-excellent-creatives "$opay_rollback_hold/public"

systemctl daemon-reload
nginx -t
systemctl reload nginx
curl -sS -o /dev/null -w '%{http_code}\n' https://ai.yingliangads.com/reports/opay-excellent-creatives/
curl -sS -o /dev/null -w '%{http_code} %{redirect_url}\n' https://ai.yingliangads.com/reports/ai-game-performance/
```

实际演练已执行 timer 的 disable/stop 与 enable/start 完整往返，恢复后两者均 enabled/active、页面仍为 200、`nginx -t` 通过。为避免人为制造公开报表中断，文件移动和 route 下线部分按已校验备份清单做命令级复核，未在生产实际执行。

## 注意事项

- 公开无鉴权是已确认产品决策；`noindex` 不等于保密。
- `latest.json` 必须最后切换。
- 最终月份冻结后禁止普通 timer 改写；修复历史数据必须显式 `--rebuild` 并保留差异审计。
- 缩略图/源文件异常不得阻塞整月；数据库、AF 配置或发布提交失败必须阻塞并保留旧版本。

## V2 发布与回滚计划追加（2026-08-27，未执行）

以上发布SHA、备份、数据版本和首次部署回滚演练均属于V1历史记录，不是当前生产状态核验，也不是V2已发布证据。本节对齐稳定接口，生产命令/回滚仍待验证；本次执行者不commit/push、不连接生产、不运行以下服务器命令。2026-08-27实现方反馈后端51项、前端行为契约34项通过，不等于生产发布或独立最终QA通过。

### V2 不变项与隔离路径

- 无MySQL DDL/DML，仍用入口`101.32.56.53:63350`并独立要求`@@read_only=1`。代理后端`@@port`不是入口端口，不能因此转用63353。
- V1 Meta/TT事实表不改列；新增独立GG表。必须使用`--clone-cache-from`创建一致性V2副本，不直接升级旧缓存，不裸拷贝仍有WAL的SQLite主文件。
- 数据范围固定`2026-01`—`2026-07`，在副本上`--google-only --refresh --rebuild`；Meta/TT原事实及既有业务字段不变。GG使用type3 video2/image4严格回连与历史FX，type0仅作平台基准。
- 公开路径、Nginx、timer/unit定义、现有env、旧AI Game Performance及主API均不改，无需Nginx reload、daemon-reload或主AI服务重启。现有env未设`OPAY_REPORT_CACHE_DB`时，新release自动使用V2默认缓存；只切代码release/静态版本，不复制示例env覆盖生产。

| 对象 | 路径约定 | 保护要求 |
| --- | --- | --- |
| GitHub精确release | `/opt/opay-excellent-creatives/releases/<V2_SHA>` | 必须来自已核验的GitHub提交；不直接SCP本地源代码替代 |
| V1源缓存 | `/mnt/data-disk/opay-excellent-creatives/cache/opay-excellent-creatives.sqlite3` | 上线前根据真实env/current核对；仅作为只读克隆来源，保留可回滚 |
| 既有数据根 | `/mnt/data-disk/opay-excellent-creatives` | env保持不变；快照以month/stage/version隔离，保留V1版本/媒体 |
| V2默认缓存 | `/mnt/data-disk/opay-excellent-creatives/cache/opay-excellent-creatives-v2.sqlite3` | 与V1文件分离；clone目标首次必须不存在，重跑不重复clone覆盖 |
| 影子公开产物 | `/mnt/data-disk/opay-excellent-creatives/staging-public-v2` | 不挂到公网Nginx；验证完整schema2七个月/媒体后才推广 |
| 正式公开目录 | `/usr/share/nginx/html/reports/opay-excellent-creatives` | 原路由保留；不可变版本文件先准备，latest最后原子替换 |
| 上线前备份 | `/mnt/data-disk/opay-excellent-creatives/backups/<timestamp>-pre-v2-<sha>` | 记录真实旧release/env/current/清单/HTML/unit/timer状态及哈希，不复用V1首次部署的absent记录 |

### V2 实施顺序与门禁

1. 实现完成后先完成本地自测及独立代码/本地QA；六项公式、null/zero、FX切换/缺失、映射重复、CLI护栏有证据。由有提交权限的负责人执行GitHub-first提交/推送并记录精确SHA，服务器fetch/checkout同一SHA到新release；本次无提交SHA可报告。
2. 现场只读记录旧`current`解析目标、env、缓存路径/版本、`latest.json`字节与数据版本、各月快照SHA、Nginx和timer/oneshot状态；核实`OPAY_REPORT_CACHE_DB`未设置且数据根与计划一致。若存在显式override则停止自动切换并核对实际路径，不静默改env。验证数据盘、空间、GitHub及63350护栏；旧文档SHA不代替现场回滚点。
3. 在任何正式切换前生成并校验完整备份。暂停的范围仅为本报表两个timer，记录原enabled/active状态；若refresh@initial/final仍运行，等待其自然完成或退出本次窗口，不强杀在途工作。使用本报表独立锁，锁占用退出75不算回填成功。
4. 一致性clone到新的V2缓存，验证`quick_check`、源快照/副本Meta/TT事实签名、冻结状态和V1表列结构；然后只在V2副本回填1—7月，不带正式`--publish`。新GG表可写，旧源缓存不得写。
5. 每月完成记录checkpoint、来源/映射/历史FX版本、原币与USD审计、快照SHA和差异。所有七个月成功后再生成影子发布产物；任一月份失败或不完整都不得将已完成子集替换线上清单。
6. 独立QA使用实际JSON及`--non-google-only`比较Meta/TT冻结业务签名，检查preserve/upgrade_audit并完成其余月份对账；另核验GG严格链路、`spend_cents>500000`、CTR严格大于、仅B、AF/安装null、六项metrics、CSV/移动端。仅FX缺口不丢完整CTR；若asset-day缺同App/账户/日Campaign基准，则该scope的CTR为null且B暂停，必须另测，不能借别账户/日期基准。
7. 通过影子门禁后准备正式不可变版本文件、缩略图和兼容schema1的V2 HTML，并在同一目标文件系统内以临时文件/目录原子替换。不能把数据盘到Web目录的跨文件系统移动当成原子操作；应先复制到目标文件系统的临时位置并校验。
8. 发布窗口内保持互斥，`current`切至V2 release，env/data-root/Nginx/timer定义保持原样；确认新代码默认缓存解析到`cache/opay-excellent-creatives-v2.sqlite3`副本，新HTML能读仍在服务的V1清单。核验全部月份schema2/snapshot SHA/preserve证据后，最后原子替换`latest.json`作为公开提交点，不能clone后未rebuild就直接publish。
9. 验证新公开清单/七个月JSON/schema2/媒体、桌面/390×844/CSV/控制台、旧系统、只读抽样；任何关键失败按下述原子回滚。只有验证成功才按原状态恢复timer，记录实际发布SHA、data_version、备份点和验证结果。

### V2 候选命令：clone、隔离回填与影子产物

以下是稳定接口的服务器Bash候选命令，仍待独立执行/发布验证；必须先替换已确认的40位GitHub SHA，核对现有env数据根及两种默认缓存名。只在已授权流程运行，不能把`--cache-db`指向V1源库。

```bash
set -euo pipefail
opay_v2_sha='<待确认的40位GitHub提交SHA>'
[[ "$opay_v2_sha" =~ ^[0-9a-f]{40}$ ]] || exit 2
opay_v2_release="/opt/opay-excellent-creatives/releases/$opay_v2_sha"
opay_v2_script="$opay_v2_release/ops/opay-excellent-creatives/opay_excellent_creatives.py"
opay_v1_cache=/mnt/data-disk/opay-excellent-creatives/cache/opay-excellent-creatives.sqlite3
opay_v2_root=/mnt/data-disk/opay-excellent-creatives
opay_v2_cache="$opay_v2_root/cache/opay-excellent-creatives-v2.sqlite3"
opay_v2_web="$opay_v2_root/staging-public-v2"
test -f "$opay_v2_script"
test -r "$opay_v1_cache"
test ! -e "$opay_v2_cache"
python3 "$opay_v2_script" --help

flock -n -E 75 /tmp/opay-excellent-creatives.lock \
  python3 "$opay_v2_script" \
  --clone-cache-from "$opay_v1_cache" --check-cache \
  --cache-db "$opay_v2_cache" --data-root "$opay_v2_root" --output-dir "$opay_v2_web"

flock -n -E 75 /tmp/opay-excellent-creatives.lock \
  python3 "$opay_v2_script" \
  --backfill --from-month 2026-01 --to-month 2026-07 --stage final \
  --google-only --refresh --rebuild \
  --cache-db "$opay_v2_cache" --data-root "$opay_v2_root" --output-dir "$opay_v2_web"

flock -n -E 75 /tmp/opay-excellent-creatives.lock \
  python3 "$opay_v2_script" \
  --backfill --from-month 2026-01 --to-month 2026-07 --stage final --publish \
  --cache-db "$opay_v2_cache" --data-root "$opay_v2_root" --output-dir "$opay_v2_web"
```

- 第一条业务命令只clone/检查；第二条刷新并重建被冻结的七个月，不发布；第三条仅把已验证快照发布到非公开影子目录，不带`--google-only`（因为它必须搭配`--refresh`）。整个代码块不写正式公开路径。
- 一旦clone成功，后续重跑不能再执行clone段或覆盖目标，应核对checkpoint后只运行缺失/待重试月份的`--month YYYY-MM --google-only --refresh --rebuild`，仍显式传V2三个路径。
- 若CLI或完整性检查失败，保留源库/旧公开版本与失败现场，先核验准确目标再决定如何处理失败的V2产物；不删除V1、不自动覆盖非空V2缓存。
- FX候选来自同日/账户的`exchange_rate`、`last_exchange_rate`，空/非法候选跳过，其他候选可核验即可；正消耗历史行缺spend_usd仍按缺口，不跳过/补0。无唯一可核验FX的非零金额fail-closed、不套当前汇率；零cost可保留0但不伪称FX已验证。素材`fx_missing_native_spend`与Campaign `platform_fx_missing_native_spend`按币种分开披露，不当USD。

### V2 验证与正式提交命令（计划，未执行）

```bash
python3 -m py_compile "$opay_v2_script"
python3 -m unittest discover -s "$opay_v2_release/ops/opay-excellent-creatives" -p 'test_*.py' -v
python3 "$opay_v2_release/ops/opay-excellent-creatives/validate_frontend_contract.py"
sqlite3 "$opay_v2_cache" 'PRAGMA quick_check;'
```

回归脚本现已提供`--non-google-only`。读取影子`latest.json`的真实`data_version`后，用实际月JSON而不是fixture自身验证：

```bash
opay_shadow_version='<影子latest.json实际data_version>'
[[ "$opay_shadow_version" =~ ^[0-9]{8}T[0-9]{12,20}[+-][0-9]{4}$ ]] || exit 2
test -f "$opay_v2_web/data/$opay_shadow_version/2026-07.json"
python3 "$opay_v2_release/ops/opay-excellent-creatives/validate_regression_snapshot.py" \
  "$opay_v2_web/data/$opay_shadow_version/2026-07.json" --non-google-only
```

默认fixture只覆盖2026-07；七个月完整对账须使用新增独立离线验收器。基线目录须为切换前冻结/备份的V1 public，含自身latest及其引用data文件，不可指向已经切V2的线上目录。以下仍为待服务器实际执行命令，不是本地合成fixture自测结果：

```bash
opay_v1_public='<本次备份清单中已核验的V1 public绝对路径>'
test -r "$opay_v1_public/latest.json"
python3 "$opay_v2_release/ops/opay-excellent-creatives/validate_v2_upgrade.py" \
  --baseline-dir "$opay_v1_public" --candidate-dir "$opay_v2_web"
```

验收器只读、不连接MySQL；退出0/JSON `status=PASS`才通过，失败退出1，参数错误退出2。须同为2026-01—07七个final月、每月六个benchmark/audit scope；Meta/TT全部原row/benchmark/audit字段仅忽略顶层metrics后完全一致，GG仅B、正整数ID、USD>5000/严格CTR/AF安装null，所有row/benchmark独立公式满足6/8位精度。摘要含逐月渠道数量、FX/映射及两类原币缺口、文件SHA；拒绝NaN/Infinity、重复键、缺scope、混schema和校验中latest改变。

上述检查不能替代源库全部候选链合法一致、历史FX原始证据、媒体/CSV和390×844移动端实测。另核对conversions有限非负小数仅详情、APM页面固定4位而CSV原精度；不改V1 fixture自证通过。正式publish的可见月份不能假设由`--from-month/--to-month`自动过滤，须以七个月验收结果和同一缓存快照清单作硬门禁。

以下正式提交命令只有在备份、七个月影子QA、互斥锁、V2 current/默认缓存解析和原子发布门禁全部满足后才可运行；env仍保持原样：

```bash
flock -n -E 75 /tmp/opay-excellent-creatives.lock \
  python3 "$opay_v2_script" \
  --backfill --from-month 2026-01 --to-month 2026-07 --stage final --publish \
  --cache-db "$opay_v2_cache" --data-root "$opay_v2_root" \
  --output-dir /usr/share/nginx/html/reports/opay-excellent-creatives
```

正式发布不得重新拉取事实或自动重算已验快照；若生成新的`data_version`，以相同snapshot SHA证明仍是验过的数据，再提交清单。`latest.json`提交前任何异常保持旧清单字节/哈希不变；提交后发现异常进入回滚，不宣称“没影响”。

### V2 原子回滚程序（待验证，不执行V1首次下线命令）

1. 保持/暂停本报表两个timer，获取同一发布锁并确认oneshot不在运行；保存失败版本、数据版本、日志及checkpoint，不强杀其他任务。
2. 从本次`pre-v2`备份清单核对旧`latest.json`、旧HTML、原release/current目标、旧env/缓存根及其SHA；确认旧清单引用的全部版本文件和媒体仍存在。缺少任一回滚对象则停止切换并报告，禁止猜测旧SHA。
3. 保持已验证兼容schema1的V2 HTML，将备份旧`latest.json`先写入正式公开目录的同文件系统临时文件，校验后原子替换正式`latest.json`。这是数据回滚提交点；不能先放回只认V1语义的旧HTML而继续暴露schema2清单。
4. 旧清单恢复后，以同目录临时文件原子恢复旧HTML；env保持未变，把`current`通过同目录临时链接原子切回原V1 release。原V1代码默认名为`cache/opay-excellent-creatives.sqlite3`，确认实际解析回该文件而不是`-v2.sqlite3`，不能仅在新代码下省略`--cache-db`冒充回滚。timer在代码/路径/公开版本一致前不得恢复。
5. V2 release、缓存、不可变data版本、快照、缩略图和审计全部保留，已打开V2页面仍可读取其固定版本；不删除V1/GG表、不倒灌V2缓存到V1、不执行Git强制回退或V1首次部署的移走公开路由操作。
6. 本流程未改unit/env/Nginx，不需daemon-reload或reload。复核原公开报表200/七个月可读、旧鉴权/API不变、缓存路径与两份数据库完整性正确，再按上线前记录恢复timer运行状态（不改其定义）。
7. 记录旧/新SHA、回滚清单哈希、开始/结束时间、命令和检查结果。命令级复核、影子实演、生产实演分别注明；本次V2未执行上述回滚，不将V1 timer演练计入V2通过数。

### V2 定时器与交付记录

- 常规定时器及env不变，仍为每月3日初版/5日终版；current切V2后使用新默认`-v2.sqlite3`执行正常全渠道刷新，切回V1代码后恢复旧默认名。不要把`--google-only`、`--clone-cache-from`或`--rebuild`常驻到timer；新月份没有冻结基线，不能用历史修正命令。
- 发布记录待负责人填写：GitHub SHA、服务器release/current、V1备份路径/哈希、V2缓存及七个月snapshot SHA、正式data_version、Meta/TT对比、FX缺口、独立QA结论、timer恢复状态、回滚验证环境及未执行项。
- 本次交付七份文档及获准新增的独立验收脚本/测试；本地自动化结果另行报告，不把“命令已列出”“回填已启动”“单月canary成功”或“影子完成”表述为七个月正式发布/最终验收成功。主线程负责提交、服务器运行及发布记录，本执行者不commit/push或部署。

## V2 实际发布记录（2026-08-27 12:09，北京时间）

本节是上述计划的执行结果；V1首次部署的下线路由命令不适用于本次回滚。

| 项目 | 实际值 |
| --- | --- |
| GitHub运行提交 | `533bbac77ae29d437d084732b7fddfc022754a93`，分支 `codex/opay-google-metrics-v2-20260827` |
| 服务器 | `43.166.187.96` / `VM-0-108-centos` |
| current目标 | `/opt/opay-excellent-creatives/releases/533bbac77ae29d437d084732b7fddfc022754a93` |
| 正式数据版本 | `20260827T120935529360+0800` |
| latest SHA256 | `f5ae1d646c8522758d23d158dec1e545aa5dc26914581dd5a18c05a493b6cecb` |
| V1回滚release | `/opt/opay-excellent-creatives/releases/0cba014b56f1c6394a9d0d3be5d735a370f83659` |
| V1回滚数据版本 | `20260826T184515557171+0800` |
| 本次备份 | `/mnt/data-disk/opay-excellent-creatives/backups/20260827-pre-google-v2-112242` |
| 备份manifest SHA256 | `23ef622485f36d2c66d78e58d925f560daf3a5afd3912ad79f005aad78131fd7` |
| 验收与操作记录 | `/mnt/data-disk/opay-excellent-creatives/qa/acceptance-20260827-v2` |

- 新release由GitHub克隆并checkout精确SHA，本地和服务器各93项测试、36项页面行为通过；最终代码以只读方式重算七个月缓存，全部入选键、基础字段和metrics与修订后影子一致。
- 备份manifest中env/Nginx/unit/timer/旧HTML/旧latest及备份文件SHA全部核验；V1在线一致性备份与新V2缓存分离，两份SQLite `quick_check=ok`。数据盘挂载UUID仍为 `3e8ac4e8-7770-456d-9e89-2ec5dd405fa8`。
- 独立七个月验收通过后，在独立锁内暂停两个timer（不disable）、原子切换current，再仅发布已验冻结快照；没有 `--refresh` 或 `--rebuild`。正式与影子所有月JSON除 `data_version` 外逐字段完全相同。正式结果192条、98个真实OPay素材ID；186条Meta/TT旧字段保持一致，新增GG月度记录6条。
- 发布命令和结果见 `promotion-command.json`、`promotion-result.json`；正式版独立复核见 `production-seven-month-upgrade.json`，仍PASS。完整V1公开JSON基线保存在验收目录 `v1-public/`，其原不可变公网文件也保留。
- 匿名HTML、latest、7个月JSON均200，无鉴权跳转，均有noindex；HTML/latest no-store，月JSON immutable。env/Nginx/unit/timer定义哈希均未改变，**没有Nginx reload、daemon-reload或主服务重启**。
- 两个timer恢复enabled/active，下一次为2026-09-03和09-05北京时间10:00（保留原随机延迟）；两个refresh oneshot无在途任务。原AI Game Performance仍302，主站仍200，8787保持监听，`nginx -t`通过。
- 回滚故障注入仅在独立影子目录完成：latest写入失败保留旧字节、先还原旧manifest再还原HTML/current后7个月可读。未为验收而把已成功上线的生产报表切回V1。浏览器原生CSV落盘位置未确认，实际七个月导出Blob/CSV内容均通过，详见测试报告。

### 本次发布的精确回滚命令

仅针对上述V2发布点；若current、配置或latest已被后续发布修改，断言会停止，须先确认新的回滚范围。保留新旧所有release、缓存和不可变月文件，不删除报表目录。以下**未在生产执行**；相同原子恢复顺序已在影子验证。

```bash
set -euo pipefail
systemctl stop opay-excellent-creatives-initial.timer opay-excellent-creatives-final.timer
flock -n -E 75 /tmp/opay-excellent-creatives.lock python3 - <<'PY'
import hashlib, json, os, pathlib, sqlite3, subprocess, tempfile, uuid
b = pathlib.Path('/mnt/data-disk/opay-excellent-creatives/backups/20260827-pre-google-v2-112242')
w = pathlib.Path('/usr/share/nginx/html/reports/opay-excellent-creatives')
c = pathlib.Path('/opt/opay-excellent-creatives/current')
m = json.loads((b / 'manifest.json').read_text())
assert str(c.resolve()) == '/opt/opay-excellent-creatives/releases/533bbac77ae29d437d084732b7fddfc022754a93'
assert json.loads((w / 'latest.json').read_text())['data_version'] == '20260827T120935529360+0800'
assert pathlib.Path(m['current']).is_dir()
for stage in ('initial', 'final'):
    unit = 'opay-excellent-creatives-refresh@%s.service' % stage
    assert subprocess.check_output(['systemctl', 'show', unit, '--property=ActiveState', '--value'], text=True).strip() == 'inactive'
for path, info in m['files'].items():
    assert hashlib.sha256(pathlib.Path(info['backup']).read_bytes()).hexdigest() == info['sha256']
    if not path.startswith(str(w)):
        assert hashlib.sha256(pathlib.Path(path).read_bytes()).hexdigest() == info['sha256']
old = json.loads((b / 'latest.json').read_text())
for month in old['months']:
    assert (w / 'data' / old['data_version'] / (month['month'] + '.json')).is_file()
db = sqlite3.connect('file:/mnt/data-disk/opay-excellent-creatives/cache/opay-excellent-creatives.sqlite3?mode=ro', uri=True)
assert db.execute('PRAGMA quick_check').fetchone()[0] == 'ok'
assert db.execute("SELECT value FROM cache_meta WHERE key='schema_version'").fetchone()[0] == '1'
db.close()
for name in ('latest.json', 'index.html'):  # manifest first; V2 HTML reads V1 safely
    data = (b / name).read_bytes()
    fd, temporary = tempfile.mkstemp(prefix='.' + name + '.rollback-', dir=str(w))
    with os.fdopen(fd, 'wb') as stream:
        os.fchmod(stream.fileno(), 0o644)
        stream.write(data); stream.flush(); os.fsync(stream.fileno())
    os.replace(temporary, w / name)
link = c.parent / ('.current-rollback-' + uuid.uuid4().hex)
os.symlink(m['current'], link); os.replace(link, c)
assert str(c.resolve()) == m['current']
assert hashlib.sha256((w / 'latest.json').read_bytes()).hexdigest() == m['files'][str(w / 'latest.json')]['sha256']
print('RESTORED', old['data_version'], str(c.resolve()))
PY
python3 - <<'PY'
import json, urllib.request
url = 'https://ai.yingliangads.com/reports/opay-excellent-creatives/'
with urllib.request.urlopen(url + 'latest.json', timeout=20) as response:
    old = json.load(response)
assert old['data_version'] == '20260826T184515557171+0800'
assert len(old['months']) == 7
for entry in old['months']:
    with urllib.request.urlopen(url + 'data/' + old['data_version'] + '/' + entry['month'] + '.json', timeout=20) as response:
        assert response.status == 200
        assert json.load(response)['schema_version'] == 1
print('ROLLBACK_PUBLIC_CHECK=PASS')
PY
systemctl start opay-excellent-creatives-initial.timer opay-excellent-creatives-final.timer
systemctl list-timers 'opay-excellent-creatives*' --all --no-pager
```

任何断言/命令失败时停止后续动作并保留现场；尤其不可在数据/current不一致时恢复调度。配置未改时无需恢复配置文件或reload；若发现配置漂移，先另行核对，不用旧备份盲目覆盖后续修改。

## Google CPC / 图片视频 CTR 发布交接（2026-08-27，未执行）

本节是新政策的待执行计划，不是发布授权/结果；本执行者没有commit/push、服务器访问、cache克隆或timer操作。上文V1/V2 SHA、manifest、命令及回滚证据只属历史，不能直接宣称本增量上线。

1. 合并Google CPC/PIC+VID规则、source6→YouTube→原type3映射修正、前端及独立验收器后，独立QA重跑最终后端/前端全量；主任务此前102项为映射修改前结果，不能作为最终门禁。
2. GitHub-first：发布负责人在批准后提交/推送并记录精确SHA，服务器仅fetch/checkout该SHA；禁止服务器先热改。现场只读核验现行V2 release、latest/HTML、env和cache路径，保存哈希/备份，不能从本文旧记录猜当前状态。
3. 保持原DATA_ROOT、旧V1/V2缓存、快照和媒体。用SQLite一致性clone（含已提交WAL）从已核验V2缓存到新`cache/opay-excellent-creatives-google-cpc.sqlite3`；新默认名按release生效，CLI/env优先级不变。若现行env显式绑定旧cache，先报告另行确认，不擅自覆盖env或旧库。
4. 在明确新cache与独立stage目录执行获准月份的Google-only refresh/rebuild，无正式publish。验证YouTube桥接和未映射PIC+VID基准，FX未知如实审计。保留旧Meta/TT所有字段/事实；不要因新排名/媒体改变旧渠道结果。
5. 对实际候选执行以下离线门禁。`--baseline-dir`必须当前V2基线，不是更早V1；下列占位符需现场填写，本次未执行：

```text
python ops/opay-excellent-creatives/validate_google_cpc_upgrade.py --baseline-dir <V2-public> --candidate-dir <google-cpc-stage> --cache-db <new-google-cpc.sqlite3> --baseline-cache <old-v2.sqlite3>
python ops/opay-excellent-creatives/validate_frontend_contract.py --payload <每个真实候选月.json>
```

6. 所有公开可见月份必须包含`selection_policy.google.version=cpc_picvid_v1`；缺月/旧政策/混版本拒绝发布。先准备不可变月数据/媒体及兼容HTML，再最后原子替换`latest.json`；任一步失败旧latest逐字节不变。须独立验证原缓存旧表哈希及Meta/TT完整字段，不仅看总行数。
7. 在独立输出目录演练提交失败与回滚，再记录真实浏览器桌面/移动端、CSV、规则说明、CPC/CTR来源及A暂停原因。正式切换/验收仅由获准发布负责人执行。

回滚目标为本次切换前保存的**当前V2** release/cache/latest/HTML，不使用上文首次V1部署的下线或固定旧SHA命令。原子恢复上一清单及兼容HTML/release指向，保留新旧不可变版本/缓存供已打开页面和排障；不得删路由、覆写旧缓存、强杀在途作业或重启主服务。现有互斥/冻结门禁不变；本增量不改任何timer、cron、其他报表、Nginx/env或调度计划。

待填写运行记录：`release_sha=PENDING`、`new_data_version=PENDING`、`candidate_manifest_sha256=PENDING`、`new/old_cache_hash_evidence=PENDING`、`independent_qa=PENDING`、`browser_csv_evidence=PENDING`、`atomic_publish/rollback_evidence=PENDING`。空缺不能记PASS。

### 主任务提供的切换前备份记录（2026-08-27）

以下由主任务现场提供，本前端/文档执行者未另行访问服务器：备份`/mnt/data-disk/opay-excellent-creatives/backups/20260827-pre-google-cpc`已创建，备份manifest SHA256=`4e97d77b1c301d249de9558963bb0bb9ca63538ed600c48d673a54cf2cdaff17`。包含公开7个月JSON、一致性`cache.sqlite3`、env/Nginx/unit/timer记录及index/latest。

切换前current=`533bbac77ae29d437d084732b7fddfc022754a93`，公开data_version=`20260827T120935529360+0800`，latest SHA256=`f5ae1d646c8522758d23d158dec1e545aa5dc26914581dd5a18c05a493b6cecb`。两个timer均enabled/active，initial/final刷新service均inactive，无env cache override。以上是回滚基线，不是新政策发布结果；切换前若状态漂移须复核，不据本文操作在途任务。
