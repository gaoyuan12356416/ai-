# 原剧封面作为AI参考图（2026-09-10）

用户确认流程：素材对应剧集 → 原剧封面 + 提示要求 → 16:9新封面 → 飞书审核。原先只有剧名/简介/要求/修改意见的纯文字输入不满足此意图。

## 实现及验收

现有按content_id+language索引查询ads_drama_resource的同一批次增加cover字段，资料匹配与封面匹配独立。相同多集封面去重；多张封面不随机取，空/非法/冲突明确提示。当前三条素材实查均能匹配各自static-v1.mydramawave.com原剧封面，未改素材SQL或宏关联规则。

创建任务只校验服务器解析结果；worker首次生成前下载图片，限制HTTPS白名单、公共IP、无跳转、15秒网络截止、8MiB、单帧JPEG/PNG/WEBP及像素上限。转为去元数据私有JPEG，记录owner/tenant资产ID和SHA。每次生成先校验并复制同一参考图到隔离工作目录，作为codex exec --image的真实附件，明确要求view_image后image_gen以referenced_image_paths调用。保留原剧人物可辨认特征，按要求重新组织16:9画面；缺图不能回退纯文生图。生成后再检查参考字节未被改写。

首次、打回、失败重试复用冻结原图；租约与版本校验防止失效worker提交。历史待生成/重做任务只在素材剧集ID与语言保持一致时补参考元数据，历史已审核上传不变。原图和生成图在审核/详情对照显示；原图缺失时本地封面功能仍可使用。

## 本地检查

service43、HTTP17、新参考工作流9通过；剧集关联21项（资料解析13项、关联工作流8项）通过，其中原图resolver覆盖语言、重复同图、多图冲突、缺失/非法。下载/生成适配器25项（Windows24通过、符号链接1项跳过，CPU Linux全部25项通过）；浏览器77项（原34+新增参考图43）通过。Python/Node语法、差异格式、公共功能guard通过。GitHub候选提交69327e8在CPU Python3.9重跑全部115项后端测试通过。

新工作流9项覆盖提交前校验且不分配短链、客户端不能伪造参考图、worker延迟下载、私有归属、冻结SHA、打回/失败重试同图、下载失败不调用生图、不发送消息、不写发布账本、旧任务补查和剧集身份变化拦截。独立代码复核通过，TLS总截止细节已修正。

## 部署

维护分支codex/youtube-cover-reference-20260910，基于已上线剧集关联07822b3。只替换drama/service/runtime、新reference模块及YouTube三个静态文件。SQL、app.py、source.py、宏模板、HK执行器、发布引擎不变。使用scripts/deploy_youtube_cover_reference.py，从GitHub准确提交归档部署，检查实时文件基线和无活动发布/生成，保存文件和SQLite备份，只重启主API/旧worker/新worker，统一writer保持运行。

验收不创建生产发布任务，不发送飞书、视频或首评；用户通过页面提交/审核产生的正常任务单独记录，不能算成测试副作用。

## 真实单图验收及证据范围

GitHub运行提交`69327e8b6723239fc86666aa55e253aa35b70c60`。CPU候选归档中运行`validate_youtube_reference_generation.py --material-id 6617770 --work-root /mnt/data-disk/deploy/youtube-auto-publish/validation/reference-69327e8-6617770`，仅读取素材和原图，在独立SQLite/资产目录进行一次生成，所有通知/发布回调禁用。

原图冻结SHA256为`9bb39cb85a13d451ad68697c77cb7b919022c962995817e1302691667a87e5be`；实际命令以`--image`携带`generation/d525bdb78e4b4a1986a56472a0901a00/v1-7c602c643249/reference-cover.jpg`，生成后SHA不变。141.266秒生成1672×941 PNG，再通过生产入库方法转为608220字节JPEG。人工对照确认保留红发精灵公主、黑发龙主、服装和熔岩黑龙，重新安排横版背景及剧名。

CLI输出确认结果来自本次原生`generated_images/01a08ac8-556c-7251-8e5c-4886bcd3834f/exec-b6f5ace1-0472-4446-ae4b-6ce92904531c.png`。追踪限制：`--ephemeral`简化JSON没有保存image_gen实参，不能宣称已审计到`referenced_image_paths`调用；验收依据是实际附件、不可变SHA、原生输出和人工对照，图像身份保留属于视觉判断。此限制不隐去，也不把提示文字单独当成工具调用证据。

验证目录保留`audit.json`、`validation-result.json`、`jpeg-validation.json`及私有stdout，图片复制到本地`output/reference-validation/`；不将可能包含模型上下文的完整trace提交GitHub。

## 生产切换（2026-09-10 18:17 CST）

CPU已从GitHub fetch并核验上述40位提交后归档，仅安装7个维护文件/10个实际文件（含3个Nginx静态副本）。准确备份`/mnt/data-disk/deploy/youtube-auto-publish/backups/reference-20260910-181726-69327e8b6723`，内含文件manifest、SQLite在线备份和部署结果。

四服务active/running、NRestarts=0；主API、旧worker、新worker加载新版，统一writer仍保持16:13启动时间。app.py摘要未变化。公网HTML/JS/CSS均200且与GitHub blob字节摘要一致，HTML版本`20260910-cover-reference-v1`；匿名bootstrap/materials仍401。SQL摘要保持`c301d02c6bfad8fa62b86cc2befe83176a38616c4d42c9e385bfb78e0e838e01`，用户五条件保留。

切换前后账本均为legacy published/published2、published/skipped1、unknown/queued2，reviewed_thumbnail published/published1；短链6条。用户此前通过页面完成的旧逻辑任务已发布且首评成功，切换时没有活动生成/上传。准备表该行原始state为uploading，界面从发布账本推导完成；不得据此误判正在上传。此次独立验收没有新增生产任务。

上线后复用该真实任务创建者的既有未过期会话，仅在进程内使用Cookie发只读GET，不生成新会话、不输出令牌。lite bootstrap 200/31ms，materials 200/4546ms，tasks 200/6ms，旧任务详情200/5ms，均no-store；这些是服务器本地HTTP请求耗时，不代表用户整页加载。6617751/6617770/6617776均matched+available，原图HTTPS、宏资料非空、link_ready=true。旧任务published/complete，reference_cover为空符合历史处理原则；读前后账本/短链/SQL不变。脱敏结果`output/postdeployment/authenticated-readonly-20260910.json`。

回滚仅恢复本次代码，保留素材SQL、图片和实际发布账本；发现后续文件漂移或活动任务拒绝覆盖：

```bash
python3 /mnt/data-disk/deploy/youtube-auto-publish/releases/69327e8b6723239fc86666aa55e253aa35b70c60/scripts/deploy_youtube_cover_reference.py --rollback /mnt/data-disk/deploy/youtube-auto-publish/backups/reference-20260910-181726-69327e8b6723
```
