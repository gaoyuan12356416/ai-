# 测试报告

## 本地验证

- `python -m py_compile app.py`：通过。
- `node --check static/quick-nav.js`：通过。
- `static/index.html` 内联脚本 `new Function` 语法解析：通过。

## 待线上验证

- 使用真实登录态打开 `https://ai.yingliangads.com/#voiceoverTasks`。
- 查询素材数：验证 `ads_drama_info.series_code` 到素材数的链路。
- 筛选素材：验证列表按素材维度展示，只有 ROAS 不达标但被补足的素材显示 `替补素材`。
- 批量创建任务：选择多个素材后逐条填写参数，确认外部接口返回 `code=0`。

## 风险

- 同一剧 ID 可能在 `ads_drama_info` 下存在多个 app/country/language 版本。当前后端默认按剧库记录取目标剧信息，可通过 `VOICEOVER_DEFAULT_APP_ID` 做部署侧优先级修正。
