# SA 测试用例评审

## 结论

通过。用例覆盖路由唯一性、双入口兼容、旧资产回滚、页面行为、正式宏闭环和生产最小部署边界。

## 评审补充

- 必须把 `/tt` 作为所有完整浏览器场景的主入口，不能只测 `/tt-code`。
- `/tt-code` 至少保留标题、动态 5 条榜单和路径不重定向的 smoke test。
- `{code}` 不能只测模板函数；必须验证自动排期生成 queue、冻结 caption、发布服务传入 GPU，以及 GPUClient 的 HTTP `title`。
- 立即测试的 409 必须断言没有 direct task 和 publish 请求。
- 生产验收必须核对旧 `tt-drama-search.js` 哈希未变化，并证明 `/opt/tt-post/current` 未切换。
- 不执行真实发布；所有 W2A 导航均拦截。
