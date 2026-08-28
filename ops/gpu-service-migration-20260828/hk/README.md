# 香港迁移实施说明

## 已确认边界

- 香港使用既有、已扩容的卷，业务统一进入 /data。卷 UUID 为 659e6f89-71fa-463d-842e-ccdf2c06e0fe；不要求 /data 是独立挂载点。
- X 原 /opt/x-post-media-repair/venv 是 FB 的间接 Python 入口，原目录与所有 FB 单位保持不变。
- 新 X：/data/x-post-media-repair。代码仍为 fba8ff603e979b443339108cb2ce45c975fbd39f，profile 仍为 v5，8820 和现有18820隧道保持。
- 广告：/data/ad-material；127.0.0.1:8796/8797，新专用隧道在 CPU 暴露18796/18797。广告源码来自美国真实在用文件，provenance.json记录哈希，未改业务逻辑。
- 运行时采用美国 Node22.22.2/Codex0.147.0，完整npm包含其原生依赖；新 Python3.9 venv，Pillow8.4.0与美国一致。
- 不迁移或操作 FB、Kronos。没有实际发帖、广告生成或 OAuth 刷新作为本阶段验证。

## 前置与准备

唯一批次：gpu-service-migration-20260828T1502。证据/备份：香港 /data/migrations/该批次/hk；源归档与传输：两端 /data/migrations/该批次/hk-inputs，权限700。

本地 relay_inputs.py prepare-inputs 只打包与转发旧runtime、广告数据、美国X历史，使用双方已验证known_hosts和显式本地key，拒绝未知hostkey。文件只经过本地内存，目标close错误必须成功处理，再比较源端、传输和目标SHA。不会复制私钥或读auth。

根协调者先统一提交/推送并在香港取得精确SHA。之后分别执行：

    python3.9 <repo>/ops/gpu-service-migration-20260828/hk/deploy.py stage --repo <repo> --sha <pushed-sha> --component x
    python3.9 <repo>/ops/gpu-service-migration-20260828/hk/deploy.py stage --repo <repo> --sha <pushed-sha> --component ad

stage验证Git HEAD/远程分支祖先关系、卷UUID、空间、路径边界及传输归档完整SHA；备份单位后生成隔离runtime/config和待安装unit。不会改current、安装unit、启停服务。每阶段写明staged_not_activated。不得把stage成功写成迁移完成。

prepare-x-history 可独立先传X历史，不中断正在传输的广告大文件。X stage完成后，先用 /data/x-post-media-repair/runtime/python/bin/python 运行 merge_x_manifests.py --with-head，记录只读筛选数量与耗时；正式导入仍在排空并停止香港X后以现场manifest为准。

X和广告均采用自己unit的/tmp与/var/tmp bind到/data，因为X子进程环境有严格白名单，不能只依赖TMPDIR。保留ProtectSystem=strict/ProtectHome。

## 统一批准后的切换

### 广告上游只读授权预检

ad_models_probe.py须先经协调者统一push，再在本地执行：

    python ops/gpu-service-migration-20260828/hk/ad_models_probe.py compare --sha <pushed-sha>

脚本校验自身与已推送Git对象一致，使用已保存known_hosts及显式本地SSH key。先在US仅提取access_token/account_id，在US发一次只读GET模型列表作为对照；US成功且目录包含gpt-5.5后，最小片段才经内存SFTP到HK本批次私有probe目录并做同请求。目录700、片段从写入起600；两端片段在finally移除，保留不含凭证的结果。不会改正式auth-source/CODEX_HOME，不复制refresh_token/id_token，不启动Codex，不登录/刷新、不生成、不允许重定向、不使用代理。每个SHA最多一次请求/主机，US对照失败则不向HK传凭证。

请求固定为当前0.147.0客户端的backend-api/codex/models与client_version参数，使用Codex exec originator、Bearer和ChatGPT-Account-Id。端点/版本/头名称依据已迁入的原生二进制核对；它是此次客户端兼容诊断，不是另建公开API服务。官方[认证说明](https://learn.chatgpt.com/docs/auth)要求把auth缓存按密码保护，且正常Codex使用可能自动刷新；因此本预检不用Codex进程。

输出只有两地HTTP码、白名单错误码、目标模型目录可见性，以及固定类别的content-type/server/page和cf-mitigated challenge布尔；不输出原始headers、title、accountID、token或响应body。HTML只在内存中做有限的标题白名单匹配；无法归类时明确保留unclassified_html，不能推断原因。模型列表成功不能替代真实素材生成验收；任何地区/账号限制须报告，不加代理绕过。最终生产auth仍须停US服务后接管。

2026-08-28 实际预检未通过：使用已推送版本 de54ca2a4577d3edb05d47aae583bb7f5c464504，每端仅一次GET。US返回200且gpt-5.5可见；HK返回403/non_json_response，无法确认模型可用。两端临时片段已删除并复核，HK正式auth与current仍不存在。响应headers、content-type、页面title和body未保留，因此现有证据不能把403定性为地区、账号或WAF限制。广告生成/视觉分析保持美国现行入口；香港只完成隔离stage，不得继续最终auth接管或activate。正式素材生成未验证。完整安全结果、证据位置和待授权诊断见 [广告访问预检报告](ad-access-preflight-report.md)。

### 服务切换

切换必须由根协调者停上游派发并确认在途为零。X脚本还检查旧work目录为空；不能绕过这个检查强停。

X activate会停止香港旧runtime，最终复制香港权威manifest，再对归档美国manifest做保守筛选和只读COS HEAD，只导入香港缺失且契约完整的v5文件。碰撞始终保留香港；不改历史profile/status；2个无status及旧profile仅归档。随后安装新unit和current并启动，记录HTTP/进程路径/临时目录真实bind证据。

    /data/x-post-media-repair/runtime/python/bin/python <control>/deploy.py activate --component x --cutover-approved gpu-service-migration-20260828T1502 --upstream-paused

统一使用上述独立venv Python执行X activate。首版7c54在调用进程内导入COS SDK，不能用未安装SDK的系统Python。执行前仍须协调者明确放行，不能因准备完成而自行切换。

香港原 x-post-media-repair-tunnel.service 有 Requires=x-post-media-repair.service，停止worker会连带停止隧道。必须在worker stop前记录隧道原active/enabled及fragment SHA；activate和rollback后只恢复原本active、且配置/enablement未变的隧道，不修改unit、key或SSH。新版控制器会自动做此恢复，缓存导入也显式调用新venv。首版7c54执行时须按以上步骤单独start既有隧道，不能把本机health当作CPU18820入口已恢复。完整验收还须在CPU检查18820 health及监听sshd对应的远端确为43.154.250.89。

广告先由根协调者停美国generation、vision及其旧隧道，确认所有共享授权使用者的协调窗口。随后依次执行本地 relay_inputs.py final-ad-data-after-source-stop 和 relay_inputs.py copy-auth-after-source-stop，最后再启动香港。增量同步对四个业务目录逐文件SHA对账，保留香港额外文件；auth仅传必要auth.json且不刷新，不得复制整个/root/.codex或动CPU截图授权。auth目标700目录/600文件，绝不进Git。activate同时要求最终数据同步及授权传输证据存在。

    python3.9 <control>/deploy.py activate --component ad --cutover-approved gpu-service-migration-20260828T1502 --upstream-paused --source-ad-stopped

启用新隧道前由根协调者核验CPU允许新反向端口且旧端口已释放；本组件不改authorized_keys、sshd或共享SSH。X美国备用服务/旧隧道由根协调者统一停止并禁用，本组件不碰其他主机unit。

verify只检查health、当前进程来源及/tmp和/var/tmp inode对应/data，不触发发布或AI生成：

    python3.9 <control>/deploy.py verify --component x
    python3.9 <control>/deploy.py verify --component ad

随后必须验证CPU真实入口、历史/files可读、请求/回执契约，以及自然业务结果。health或stage成功不能替代端到端验收。

## 回滚

先停上游并排空。运行相同显式门禁的rollback，恢复本批次备份unit；如果unit被他人更新，拒绝覆盖。广告回滚只停新香港服务，不会自动恢复美国服务，根协调者确认香港已停后再按源端备份恢复单一生产入口。

    python3.9 <control>/deploy.py rollback --component x --cutover-approved gpu-service-migration-20260828T1502 --upstream-paused
    python3.9 <control>/deploy.py rollback --component ad --cutover-approved gpu-service-migration-20260828T1502 --upstream-paused

所有新/data历史、生成结果和manifest保留。X回滚恢复旧unit时新运行期间新增manifest仍在/data，重新开放修复请求前做差异对账；不可直接覆盖旧manifest或删除新缓存。

## 本地验证

    python -m unittest discover -s ops/gpu-service-migration-20260828/hk/tests -v

用隔离目录验证卷身份/空间失败、路径逃逸、archive越界、cutover门禁、manifest碰撞保护/HEAD门禁/幂等、源文件哈希和历史URL契约。测试不接生产、不使用真实凭证。
