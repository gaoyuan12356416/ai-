# API：Meta 剧集广告删除 V2

前缀 `/api/fb-post-ad-delete`。全部需真实后台Cookie和`fb_ad_asset_delete`；产品目录/任务读取/执行同时验证产品ACL。POST为同源application/json，最大64KiB。

| 方法 | 路径 | 行为 |
|---|---|---|
| GET | /products | 返回items：id,name,kind(App/W2A),parent_id,parent_name；无默认产品 |
| POST | /preview | `{input_type,ids,product_ids}`；202返回previewing任务，后台冻结清单 |
| GET | /jobs | 最新任务摘要，新台账100条与旧只读任务合并，普通用户仅自己 |
| GET | /jobs/{job_id} | page(默认1)、page_size(默认50，最大100)、kind、status；返回分页objects及total |
| POST | /jobs/{job_id}/execute | `{preview_id,phases,request_id}`；202返回job_id/run_id/status/duplicate |
| POST | /jobs/{job_id}/reconcile | `{preview_id}`；200返回checked/read_only；每次GET核实1个unknown，优先最久未核实对象 |
| POST | /delete-posts、/delete-ads | 410，旧删除接口关闭 |

## 请求示例
```json
{"input_type":"content_id","ids":["66075322"],"product_ids":["3443","3543"]}
```
```json
{"preview_id":"<frozen-preview-id>","phases":["creative","ad","video"],"request_id":"<unique-uuid>"}
```
执行请求不接受任意对象追加。同一个request_id重发返回原run；改变任务/阶段/preview_id则409。所有删除范围由服务端台账决定。

## 任务与对象
任务状态previewing/ready/failed/running/completed/partial/interrupted。DTO包含products、dramas、blockers、summary、phase_results、current_phase、runs；分页objects不改变全量统计。对象包含key(kind:id)、object_id、kind、status、产品/剧/资源/语言/账户ID列表、reason、result。Token不返回也不持久化。

对象状态：pending待执行，deleted明确删除成功，already_deleted明确读回DELETED，failed明确失败，blocked归属或核验阻止，unknown结果待核实，in_progress正在处理。请求超时/连接异常/5xx/非法响应均不判成功。unknown只接受明确删除证据解除锁，不能直接改成pending。

历史任务schema_version=1/read_only=true；保留原summary及legacy_items/legacy_logs，不能调用V2 execute继续。

## 错误
统一`{error,message}`；401登录失效，403模块/产品权限，400输入/限额，404任务不存在，409预览或请求冲突，410旧入口关闭，429预览并发上限，503台账/只读源/数据盘不可用。读取异常不会返回空成功清单。
