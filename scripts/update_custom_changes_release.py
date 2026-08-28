#!/usr/bin/env python3
from pathlib import Path

path = Path('CUSTOM_CHANGES.md')
text = path.read_text(encoding='utf-8')


def replace_once(old: str, new: str) -> None:
    global text
    count = text.count(old)
    if count != 1:
        raise SystemExit(f'expected exactly one match, got {count}: {old[:80]!r}')
    text = text.replace(old, new, 1)


replace_once(
    '11. fix_missing_upload_template_safety.py\n12. fix_recordings_browser.py\n```',
    '11. fix_missing_upload_template_safety.py\n12. fix_recordings_browser.py\n13. fix_manual_upload_feedback.py\n```',
)

replace_once(
    '''最关键的未来兼容保险：\n\n```text\nawait onOk({ ...entity, ...cleanValues })\n```\n\n即配置覆写弹窗只修改它知道的字段，未触碰的主播顶层字段从原始 `entity` 继承，防止以后官方新增字段而弹窗没同步时再次被“漏传清空”。''',
    '''### PR #18 对该方案的进一步修正\n\nPR #16 曾为了避免漏字段被清空，让 `OverrideModal` 保存时把原始主播 `entity` 整体合并回请求。后续实测证明这个方案仍然不安全：前端 `entity` 可能来自旧 SWR 缓存，或者其中已经带有运行时/继承后显示出来的值。只要用户打开“配置覆写”并保存一个与主字段无关的选项（例如 `douyin_danmaku`），旧的 `filename_prefix` 等字段就可能被顺带写回数据库。\n\n因此 PR #18 **废弃全量回传 `...entity`**。现在“配置覆写”必须使用真正的 patch-only 请求：\n\n```text\n{\n  id: entity.id,\n  override: cleanValues.override\n}\n```\n\n也就是说，扳手弹窗只允许修改 `override`，绝不能顺手提交：\n\n```text\nfilename_prefix\nupload_streamers_id\npostprocessor\nformat\ntime_range\nremark\nurl\n或任何其它主播主字段\n```\n\n主播主字段由“录播管理”弹窗维护；未知/未来字段则由后端 partial-update 语义负责保留。''',
)

new_sections = r'''
## 4.15 “配置覆写”严格隔离为 `id + override`

**PR #18（2026-08-28）**

修改器：

```text
scripts/fix_override_streamer_fields.py
```

主要修改官方文件：

```text
app/ui/OverrideModal.tsx
```

### 事故触发方式

用户不需要修改“文件名模板”。仅执行：

```text
录播管理 -> 扳手“配置覆写” -> 开启 douyin_danmaku -> 保存
```

就可能导致单主播 `filename_prefix` 被自动填成 `/recordings/...` 的完整模板。

弹幕开关本身不是根因；它只是触发了一次保存。真正的根因是覆写弹窗把整个主播 `entity` 一起 PUT 回后端。

### 当前硬性规则

“配置覆写”的请求 payload 只能包含：

```text
id
override
```

开启/关闭弹幕、改画质、平台参数等，只能改变 `override` JSON。不得污染“录播管理”维护的字段。

### 和后端 partial update 的关系

PR #16 已让 `PUT /v1/streamers` 支持“未传字段保留”；因此前端没有任何理由再全量回传主播对象。

这两层必须同时保留：

```text
OverrideModal -> 只发 id + override
后端          -> 未出现的字段保留原值
```

### 回归测试

```text
tests/test_config_safety_customizations.py
```

以后如果看到 `OverrideModal` 再出现类似：

```text
{ ...entity, ...values }
```

必须视为高风险回归。

---

## 4.16 手动投稿必须可观测，禁止“点了没反应”

**最终整合：PR #20（2026-08-28）；PR #19 为合并前的独立验证分支。**

修改器：

```text
scripts/fix_manual_upload_feedback.py
```

修改官方文件：

```text
app/(app)/upload-manager/page.tsx
crates/biliup-cli/src/server/api/endpoints.rs
```

Marker：

```text
biliup-custom:manual-upload-feedback:v1
```

### 原问题

投稿管理“小飞机”手动上传的官方 UI：

```text
选择文件 -> POST /v1/uploads -> 关闭弹窗
```

没有明确的成功提示、失败提示和未选文件提示；后端又会把真正上传放进后台 `tokio::spawn`，因此用户很容易看到“点击上传后什么都没有发生”。

另外，如果投稿模板 uploader 是 `Noop`，旧后端会静默返回成功，但实际上根本不会向 B站上传。

### 当前前端行为

```text
没有选择文件
-> warning：请至少选择一个录像文件

请求在进入后台任务前被后端拒绝
-> error：显示后端错误

后端接受任务
-> success：上传任务已提交，并显示文件数量
```

成功提示只代表：

```text
/v1/uploads 已受理，后台上传任务已创建
```

**它不等于 B站最终 `Submit successful`。** 真正是否上传/投稿成功仍以实时日志和最终 B站返回为准。

### 当前后端行为

- Noop 投稿模板：返回 HTTP 400，不再静默成功；
- 开始手动上传日志必须带 `template_id` 和 `file_count`；
- 后台失败日志也必须带 `template_id` 和 `file_count`；
- 受理响应包含 `accepted=true / template_id / file_count`。

### 回归测试

```text
tests/test_manual_upload_feedback_modifier.py
```

### 维护不变量

任何未来 UI 重构都必须保证：

```text
没选文件有提示
同步请求错误有提示
Noop 不得伪装成功
任务受理有提示
受理 != 最终 B站投稿成功
日志能定位 template_id + file_count
```

---

'''
replace_once('\n# 5. `filtering_threshold`：我们明确保持官方行为\n', '\n' + new_sections + '# 5. `filtering_threshold`：我们明确保持官方行为\n')

replace_once(
    'biliup-custom:recordings-upload-picker:v1\n```',
    'biliup-custom:recordings-upload-picker:v1\nbiliup-custom:manual-upload-feedback:v1\n```',
)

replace_once(
    '''tests/test_recordings_browser_modifier.py\n  -> /recordings 递归列表、历史播放、手动投稿、路径安全\n''',
    '''tests/test_recordings_browser_modifier.py\n  -> /recordings 递归列表、历史播放、手动投稿、路径安全\n\ntests/test_manual_upload_feedback_modifier.py\n  -> 手动投稿未选文件/Noop/请求错误/受理提示与日志可观测性\n''',
)

replace_once(
    '''PR #16 的三个安全层就是为阻止这类事故再次发生：\n\n```text\n前端字段/原 entity 合并\n+ 后端 partial update\n+ 无投稿模板时跳过 destructive postprocessor\n```\n\n以后任何重构都不能只看“页面能保存、代码能编译”，必须专门验证这三层。''',
    '''现在阻止同类事故的安全层是：\n\n```text\n前端字段名与后端 schema 对齐\n+ OverrideModal 只提交 id + override（PR #18）\n+ 后端 partial update：未传字段保留\n+ 用户主动清空时显式发送 null\n+ 无投稿模板时跳过 destructive postprocessor\n```\n\n以后任何重构都不能只看“页面能保存、代码能编译”，必须专门验证这些层，而且禁止重新引入 `...entity` 全量回传。''',
)

replace_once(
    'PR #17 2026-08-28  可选主播字段主动清空发送 null，恢复单主播继承全局配置\n```',
    'PR #17 2026-08-28  可选主播字段主动清空发送 null，恢复单主播继承全局配置\nPR #18 2026-08-28  配置覆写只提交 id + override，禁止污染主播主字段\nPR #20 2026-08-28  手动投稿显式反馈 + Noop 拒绝 + template_id/file_count 日志（#19 为独立验证 PR）\n```',
)

replace_once(
    '''配置覆写不能清 upload_streamers_id\n漏传 JSON 字段必须保留原值\nUI 主动清空可选主播字段必须显式发送 null\n显式 null 才允许清空''',
    '''配置覆写不能清 upload_streamers_id\n配置覆写只能提交 id + override，禁止全量回传 entity\n漏传 JSON 字段必须保留原值\nUI 主动清空可选主播字段必须显式发送 null\n显式 null 才允许清空\n手动投稿 Noop 不得静默成功，任务受理必须有 UI 提示和 template_id/file_count 日志''',
)

replace_once(
    '当前文档对应自定义分支：`fix/clear-optional-streamer-fields`（PR #17，合并后以 `main` 为准）。',
    '当前文档对应最终发布候选：`release/final-20260828`（PR #20，合并后以 `main` 为准）。',
)

path.write_text(text, encoding='utf-8')
print('CUSTOM_CHANGES.md updated for PR #18/#20')
