# Video 删除凭证适配

## 背景及核实结论

用户要求核实失败 Video 的 Page/上传身份并适配删除凭证，同时保留“筛选到固定 Video ID 即执行、不要读取门禁”的规则。当前失败请求使用内部用户631的User Token，17条均收到Meta200/HTTP403 publish_actions废弃提示。

已逐条只读核实17个Creative：object_story_spec.video_data.video_id精确等于对应失败Video，关联Page均为MoboReels Drama（1151354758071142）。这些源Video与effective_object_story_id对应的Page视频是不同对象。关联Page不等于已证明源视频上传者；源Video.from缺失，permalink中的另一身份无法读取或匹配现有授权库，上传身份仍未证实。

已有Page凭证经GET /me确认身份与上述Page一致，授权来源Meta用户122094689414913447映射内部用户709，属于原冻结候选用户。Page状态-1按现有授权规则可作为候选，只有1被排除；读取成功不保证DELETE权限。

## 最终行为

1. 固定Video ID、剧/产品范围及阶段选择保持；禁止改删另一个Page视频。
2. 优先复用本对象历史记录的Page关联并动态取Token；否则有限读取Video.from和冻结Creative的精确关系来选择Page或上传User凭证。
3. 从Page授权表动态读取，只使用冻结候选内部用户关联的Meta身份；不同ID口径明确区分，Page间不复用缓存Token。
4. 元数据读取和Page查找均不构成删除门禁，缺失/失败时回退原User Token继续单次DELETE；没有任何可用凭证时明确失败。
5. 选择凭证后先将非秘密身份写入持久化尝试及审计，再DELETE。Token只在内存中。未知结果及进程中断保留身份和全局锁。
6. 不自动轮换Token重发；单失败继续后续对象，成功跳过、unknown不重试，Creative→Ad→Video顺序保持。
7. 页面诊断展示凭证类型、Page、授权记录、Meta用户、内部用户、关联依据；不新增开关或额外确认。
8. 正常Cookie、模块权限、产品ACL、冻结清单、并发与台账可写性约束保持。生产验收仅GET，不执行真实DELETE。
