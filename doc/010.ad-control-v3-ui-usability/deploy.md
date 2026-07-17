# 部署文档

## 变更内容

V3 搜索单选、首屏减负、估算步骤、保存试算导航、短缓存、Dramawave 具体投放产品和 FB 范围解析。

## 配置项

```dotenv
AD_CONTROL_V3_META_CACHE_TTL_SECONDS=60
```

允许 15～300 秒；未配置默认 60。

## 数据库变更

- 无 DDL。
- 向 `ads_ai.ad_control_v3_product_catalog` upsert 具体投放产品。
- 上线前导出全表 SQL/TSV/hash；真实写入后不删审计目录，回滚时停用本批 `catalog_kind=delivery_product` 行或恢复备份。

## 部署步骤

1. 本地全量测试并提交/push 精确 commit。
2. 服务器备份 `app.py`、`features/ad_control_v3`、静态 overlay、systemd/env、runner、当前 catalog 导出和 hash。
3. 从 GitHub 获取精确 commit，生成发布包；与线上共享 monolith 做精确 overlay，不整份覆盖无关文件。
4. 在服务器执行同步脚本 dry-run：

```bash
python3 scripts/sync_ad_control_v3_delivery_products.py \
  --platform-app-id 1031273318485141 \
  --canonical-product Dramawave \
  --insight-product Dramawave
```

5. 审核输出必须为 `count=129`，记录 `plan_hash`；再带相同 count/hash 执行 `--apply`。
6. 63350 回读 129 个 `catalog_kind=delivery_product`、唯一 selector、App ID 证据。
7. 原子替换代码/静态资源，设置 TTL，`python -m py_compile` 和 `node --check`。
8. 重启 `drama-material-api.service`，不改 runner/live feature flags。
9. 生产浏览器按 TC-001/005/007/008/011～016 验收。

## 验证步骤

- `/api/ad-control/v3/meta` 返回 129 条 Dramawave 具体产品，管理员优化师仍 394 条但关闭菜单时 DOM 不渲染其 options。
- 连续请求 `/meta` 观察服务日志无异常，第二次使用缓存。
- 具体 W2A 1 天范围估算成功；日志产品展示友好名，内部保留稳定 selector。
- 新建停用 observe 规则保存+试算后返回列表；执行日志有记录，Meta 写为 0。
- V2 页面、V3 live pause/copy guard 和现有规则均正常。

## 回滚方案

1. 先关闭本次新增具体产品入口：将本批目录 `enabled=0`，不影响旧 15 产品和 live 总开关。
2. 恢复代码/静态 overlay/env 到本次备份并重启 API。
3. 若尚未写目录，可直接停止；若已写真实记录，不删除，保留审计并停用。
4. 已由旧/新规则触发的 Meta 对象不做数据库回滚；按现有 execution/lineage 精确 PAUSE 隔离。

## 注意事项

- 同步脚本默认只读；禁止省略 expected-count/hash 直接写。
- 目录写入必须使用 63353，回读使用 63350；源库只读 63350。
- 不重放 runner，不扩大 live 操作目标。
