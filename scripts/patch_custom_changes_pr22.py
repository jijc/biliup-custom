#!/usr/bin/env python3
from pathlib import Path

PATH = Path("CUSTOM_CHANGES.md")
MARKER = "biliup-custom:submit-pipeline-recovery:v1"


def replace_once(text: str, old: str, new: str, label: str) -> str:
    count = text.count(old)
    if count != 1:
        raise SystemExit(f"{label}: expected exactly one anchor, got {count}")
    return text.replace(old, new, 1)


def main() -> None:
    text = PATH.read_text(encoding="utf-8")
    if MARKER in text:
        print("CUSTOM_CHANGES.md already contains PR #22 documentation")
        return

    text = replace_once(
        text,
        "15. fix_submit_timeout.py\n```",
        "15. fix_submit_timeout.py\n16. fix_submit_pipeline_recovery.py\n```",
        "modifier order",
    )

    section = r'''## 4.19 上传完成但稿件未创建：封面阶段保护 + 最终投稿安全恢复

**PR #22（2026-08-28）**

修改器：

```text
scripts/fix_submit_pipeline_recovery.py
```

依赖顺序：

```text
fix_submit_timeout.py
-> fix_submit_pipeline_recovery.py
```

修改官方文件：

```text
crates/biliup-cli/src/server/common/upload.rs
```

Marker：

```text
biliup-custom:submit-pipeline-recovery:v1
```

### 这次真实事故

手动投稿 5P 时，日志已经明确出现 5 次：

```text
Upload completed: ...
```

其中最后一个 P 也已经完成上传，但之后长时间没有：

```text
ResponseData { code: 0, ... }
Web 接口投稿成功
Submit successful
```

同时 B站创作中心/稿件管理中确认**没有对应稿件**。

因此这次不是“大文件没传完”，而是：

```text
媒体分P已经上传到 B站上传存储
-> 稿件构建/最终 create-submit 阶段没有完成
-> B站没有真正生成稿件
```

### 为什么 PR #21 的 90 秒 submit timeout 还不够

最后一个媒体文件 `Upload completed` 后，并不是马上调用最终 submit。官方链路还会先执行：

```text
build_studio()
```

如果投稿模板设置了自定义封面，其中还会执行：

```text
bilibili.cover_up(...).await
```

原代码这个封面网络请求没有完整 timeout，因此它也可能造成：

```text
最后一个 P 已上传
-> 封面上传卡住
-> 根本还没走到最终 submit
```

而且封面读取/上传失败时，原代码可能继续保留本地文件路径作为 `studio.cover`，对最终投稿也不安全。

### 封面阶段现在的行为

自定义封面上传增加：

```text
30 秒 timeout
```

并增加明确阶段日志：

```text
开始构建B站稿件信息
开始上传B站封面
B站封面上传成功
B站稿件信息构建完成
```

如果封面文件读取失败、B站封面上传明确失败，或者 30 秒仍没有返回：

```text
清空 studio.cover
继续无自定义封面投稿
```

日志分别会明确显示失败或超时。这里选择“无封面继续投稿”，是因为媒体内容本身已经上传完成，不能因为一个可降级的封面请求让整份稿件永远卡死。

### 最终 submit 超时后的安全恢复

PR #21 的最终 submit 仍保持：

```text
90 秒 timeout
```

但 PR #22 不再在 timeout 后立即结束并丢失内存中的已上传 `Video` 信息。

只有发生“网络 timeout、结果不确定”时，才进入远端确认：

```text
status = is_pubing,pubed,not_pubed
从第 1 页开始检查最近 2 页
最多检查 3 次
每次查询最多 10 秒
两次查询之间等待 5 秒
```

匹配条件要求：

```text
稿件标题与当前 studio.title 完全一致
且 ctime 位于本次 submit 开始时间附近
```

#### A. 查到对应稿件

如果远端稿件列表已经出现对应 `aid/bvid`：

```text
已从B站稿件列表确认稿件存在
Submit successful
```

此时不再重复 submit，避免产生重复稿件。

因为已经由 B站稿件列表确认真实稿件存在，自动录制链路可继续按“成功”处理，随后才允许执行既有成功 postprocessor（包括用户明确配置的 `rm`）。

#### B. 至少两次远端查询成功，并且都没有对应稿件

这时才允许：

```text
复用本次已经上传好的 Video token / Studio
只重试最终 create-submit 一次
```

关键点：

```text
不会重新上传 01/02/03/04/05 大视频文件
```

第二次最终 submit 仍然有 90 秒 timeout；如果再次 timeout，会重新检查远端状态。如果仍已确认没有稿件，则返回错误，不再做第三次提交。

#### C. 远端稿件状态查不清

例如查询接口本身失败或超时，无法得到至少两次可信的“没有稿件”结果：

```text
远端稿件状态无法确认，不自动重试
```

任务返回错误，保留本地媒体。宁可人工检查，也不能为了自动恢复冒险制造重复稿件。

### 哪些错误绝对不会自动重试

只有 `tokio::time::timeout` 造成的“结果未知”才进入恢复流程。

如果 B站已经明确返回错误，例如：

```text
21566 投稿过于频繁
```

或者任何其它明确接口错误：

```text
Ok(Err(err)) -> 直接返回错误
```

不会自动重试，也**没有 App -> Web fallback**。

### 文件安全边界

如果最终仍未确认成功：

```text
submit_to_bilibili() 返回 Err
-> 不进入成功 postprocessor
-> postprocessor=rm 不执行
-> 本地录像保留
```

只有以下两种情况会被视为成功：

```text
1. 最终 submit 直接返回成功
2. submit 响应超时，但随后从 B站远端稿件列表确认真实 aid/bvid 已存在
```

### 对旧失败任务的限制

这套恢复机制能复用的是**当前仍在运行的那次任务内存中的 Video token**。

对于 PR #22 发布之前已经结束、重启或丢失上下文的旧任务，无法从本地 MP4 反推出之前那次 Upos 上传产生的完整 Video token。因此旧的 5 个本地 MP4 仍需要重新上传一次；新版本会保护这次新的任务，不再因为最终 submit timeout 而无意义地第二次上传 5 个大文件。

### 回归测试

```text
tests/test_submit_pipeline_recovery_modifier.py
```

覆盖：

- 封面 30 秒 timeout；
- 封面失败/超时清空 cover 并继续；
- build_studio 阶段日志；
- timeout 后远端全状态稿件确认；
- 至少两次成功 negative check 才允许最终 submit 重试；
- 复用已上传 Video 信息，仅最终 submit 最多 2 次；
- B站明确错误不自动重试；
- 不引入 App -> Web fallback；
- modifier 幂等；
- 四条 build/validate/publish 链路必须按顺序接入。

### 同步官方时重点检查

如果官方以后原生提供：

- cover upload 完整 timeout/降级；
- 最终 submit 的幂等机制；
- 上传完成后的稿件状态确认；
- 或持久化的“只重试 submit、不重传媒体”能力；

应先人工比较语义，再决定删除/缩减本 modifier，不能叠加两套重试机制。

---

'''
    text = replace_once(
        text,
        "# 5. `filtering_threshold`：我们明确保持官方行为",
        section + "# 5. `filtering_threshold`：我们明确保持官方行为",
        "PR22 section insertion",
    )

    recovery_safety = r'''## 6.6 上传完成后的稿件恢复边界

`Upload completed` 只证明媒体 P 上传完成，不代表稿件 create-submit 已成功。

当前安全顺序：

```text
所有媒体 P Upload completed
-> build_studio
-> 自定义封面最多 30 秒，失败则无封面继续
-> 最终 submit 最多 90 秒
-> timeout 才查询远端稿件列表
-> 查到 aid/bvid：视为真实成功，不重复提交
-> 至少两次成功查询均未找到：只重试最终 submit 一次，不重传媒体
-> 查询状态不可靠：不自动重试
-> B站明确错误（含 21566）：不自动重试
```

如果最终返回错误，自动链路不得执行成功 postprocessor，因此本地录像继续保留。

'''
    text = replace_once(
        text,
        "因为 timeout 不能证明远端一定没有处理请求，重新点“小飞机”或重新触发自动投稿前，应先检查 B站创作中心是否已经有对应稿件/BV。\n\n---\n\n# 7. 当前明确“不做”的功能",
        "因为 timeout 不能证明远端一定没有处理请求，PR #22 会先自动检查 B站远端稿件状态，再决定是否允许只重试最终 submit。\n\n" + recovery_safety + "---\n\n# 7. 当前明确“不做”的功能",
        "submission safety section",
    )

    text = replace_once(
        text,
        "biliup-custom:submit-timeout:v1\n```",
        "biliup-custom:submit-timeout:v1\nbiliup-custom:submit-pipeline-recovery:v1\n```",
        "marker list",
    )

    text = replace_once(
        text,
        "tests/test_submit_timeout_modifier.py\n  -> B站最终投稿 90 秒超时、阶段日志、三种提交分支保持、无自动 fallback、幂等\n",
        "tests/test_submit_timeout_modifier.py\n  -> B站最终投稿 90 秒超时、阶段日志、三种提交分支保持、无自动 fallback、幂等\n\ntests/test_submit_pipeline_recovery_modifier.py\n  -> 封面 timeout/降级、远端稿件确认、只重试最终 submit、四条构建链路接入、幂等\n",
        "test responsibility list",
    )

    text = replace_once(
        text,
        "- B站最终 create/submit 的完整超时与阶段日志。\n",
        "- B站最终 create/submit 的完整超时与阶段日志；\n- 封面上传 timeout/降级与最终 submit 超时后的远端确认/单次恢复。\n",
        "upstream native checklist",
    )

    old_final = '''### 最终投稿

```text
所有 P 的 Upload completed != 稿件提交成功
最终 create/submit 最多等待 90 秒
超时必须返回错误并阻断成功 postprocessor/rm
超时只表示“未确认提交成功”，不能断言远端一定没收到
超时后重试前先检查 B站创作中心，避免重复稿件
不得因此引入 21566 App -> Web 自动 fallback
```
'''
    new_final = '''### 最终投稿

```text
所有 P 的 Upload completed != 稿件提交成功
build_studio 的自定义封面上传最多等待 30 秒，失败/超时清空 cover 后继续
最终 create/submit 每次最多等待 90 秒
只有 timeout 这种“结果未知”允许进入远端确认
远端确认覆盖 is_pubing,pubed,not_pubed
查到本次对应 aid/bvid 后不得重复 submit
至少两次成功查询都未找到，才允许复用已上传 Video 信息重试最终 submit 一次
远端状态查不清则不自动重试
B站明确错误（包括 21566）不得自动重试
最终失败必须阻断成功 postprocessor/rm
不得因此引入 21566 App -> Web 自动 fallback
```
'''
    text = replace_once(text, old_final, new_final, "final submit invariants")

    text = replace_once(
        text,
        "PR #21 2026-08-28  实时日志 WebSocket 心跳/重连 + 最终投稿 90 秒超时与阶段日志\n```",
        "PR #21 2026-08-28  实时日志 WebSocket 心跳/重连 + 最终投稿 90 秒超时与阶段日志\nPR #22 2026-08-28  上传完成无稿件恢复：封面 timeout/降级 + 远端确认 + 最终 submit 单次重试\n```",
        "PR timeline",
    )

    text = replace_once(
        text,
        "当前文档对应发布候选：PR #21 `fix/ws-log-heartbeat-reconnect`（合并后以 `main` 为准）。",
        "当前文档对应发布候选：PR #22 `fix/submit-pipeline-recovery`（合并后以 `main` 为准）。",
        "release candidate",
    )

    PATH.write_text(text, encoding="utf-8")
    print("CUSTOM_CHANGES.md patched for PR #22")


if __name__ == "__main__":
    main()
