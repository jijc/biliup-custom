#!/usr/bin/env python3
from pathlib import Path

PATH = Path("CUSTOM_CHANGES.md")


def replace_once(text: str, old: str, new: str, label: str) -> str:
    count = text.count(old)
    if count != 1:
        raise SystemExit(f"{label}: expected exactly one anchor, got {count}")
    return text.replace(old, new, 1)


def main() -> None:
    text = PATH.read_text(encoding="utf-8")
    if (
        "15. fix_submit_timeout.py" in text
        and "## 4.18 B站最终投稿 90 秒超时与阶段日志" in text
        and "biliup-custom:submit-timeout:v1" in text
    ):
        print("CUSTOM_CHANGES.md already patched for PR #21")
        return

    text = replace_once(
        text,
        "14. fix_log_websocket_resilience.py\n```",
        "14. fix_log_websocket_resilience.py\n15. fix_submit_timeout.py\n```",
        "modifier order",
    )

    submit_section = r'''## 4.18 B站最终投稿 90 秒超时与阶段日志

**PR #21（2026-08-28）**

修改器：

```text
scripts/fix_submit_timeout.py
```

修改官方文件：

```text
crates/biliup-cli/src/server/common/upload.rs
```

Marker：

```text
biliup-custom:submit-timeout:v1
```

### 原问题

多 P 手动投稿或自动投稿时，所有视频文件都可能已经出现：

```text
Upload completed: ...
```

但文件上传完成后还必须执行一次最终的 B站 create/submit 请求，只有这一步成功才算稿件真正提交。官方 HTTP client 目前只有连接超时，缺少覆盖整个最终投稿 future 的明确上限；如果连接已经建立但响应长期不返回，日志可能在最后一个 `Upload completed` 后长时间没有任何成功或失败结论。

### 当前行为

`submit_to_bilibili()` 在真正调用 B站最终投稿接口前先输出：

```text
开始提交B站稿件
```

并带上：

```text
submit_api
title
part_count
timeout_secs
```

最终投稿 future 统一由 Tokio timeout 包裹：

```text
90 秒
```

适用于当前已有的：

```text
App
Web
BCut Android
```

三种提交方式。这个修改**不改变提交方式选择，也没有实现 App -> Web 自动 fallback**。

### 超时语义

超过 90 秒仍没有拿到最终响应时：

```text
ERROR B站最终投稿请求超时
```

任务返回错误，错误文案明确使用：

```text
文件上传已完成但稿件未确认提交成功
```

这里故意写“未确认”，而不是“投稿失败”。网络超时存在不确定性：远端可能在客户端放弃等待前后已经收到请求。因此发生超时时，**先到 B站创作中心检查是否已经出现稿件/BV，再决定是否重新投稿**，避免重复稿件。

### 文件安全边界

自动投稿链路中，`submit_to_bilibili()` 的错误会直接向上返回，因此：

```text
最终投稿超时/失败
-> 不进入成功后的 postprocessor
-> 如果配置 postprocessor=rm，也不能因为这次超时删除本地录像
```

这和既有 21566 安全语义一致：只有最终投稿成功后，才允许继续成功后的后处理。

手动投稿本身没有自动 `rm` 成功后处理；后台任务仍会通过 `template_id + file_count` 的安全日志定位任务。不要把完整上传 `error_stack` Debug 持久化到 Web 日志，因为官方上传错误可能包裹短期上传授权信息。

### 成功判据

90 秒超时保护不会降低成功标准。仍必须看到 B站正常返回以及：

```text
Web 接口投稿成功   # submit_api=web 时
Submit successful
```

才能确认稿件提交成功。仅有各 P 的 `Upload completed` 仍然不等于投稿成功。

### 回归测试

```text
tests/test_submit_timeout_modifier.py
```

覆盖：

- 最终投稿 90 秒上限；
- 投稿开始阶段日志；
- 超时错误语义；
- App/Web/BCut 三条原有分支各保留一次；
- 禁止借此偷偷加入 App -> Web fallback；
- modifier 幂等执行。

### 同步官方时重点检查

如果官方以后已经为最终 create/submit 提供等价的完整请求超时与阶段日志，应触发 `42 / native-review` 并人工评估删除本 modifier，避免双重 timeout。

---

'''
    text = replace_once(
        text,
        "# 5. `filtering_threshold`：我们明确保持官方行为",
        submit_section + "# 5. `filtering_threshold`：我们明确保持官方行为",
        "submit section insertion",
    )

    text = replace_once(
        text,
        "biliup-custom:log-websocket-resilience:v1\n```",
        "biliup-custom:log-websocket-resilience:v1\nbiliup-custom:submit-timeout:v1\n```",
        "marker list",
    )

    text = replace_once(
        text,
        "tests/test_log_websocket_resilience_modifier.py\n  -> 实时日志 25 秒心跳、预期断连降级、3 秒自动重连、stale socket 防护\n",
        "tests/test_log_websocket_resilience_modifier.py\n  -> 实时日志 25 秒心跳、预期断连降级、3 秒自动重连、stale socket 防护\n\ntests/test_submit_timeout_modifier.py\n  -> B站最终投稿 90 秒超时、阶段日志、三种提交分支保持、无自动 fallback、幂等\n",
        "test responsibility list",
    )

    text = replace_once(
        text,
        "- 实时日志 WebSocket 心跳/自动重连。\n",
        "- 实时日志 WebSocket 心跳/自动重连；\n- B站最终 create/submit 的完整超时与阶段日志。\n",
        "upstream native checklist",
    )

    final_submit_check = r'''### 最终投稿

```text
所有 P 的 Upload completed != 稿件提交成功
最终 create/submit 最多等待 90 秒
超时必须返回错误并阻断成功 postprocessor/rm
超时只表示“未确认提交成功”，不能断言远端一定没收到
超时后重试前先检查 B站创作中心，避免重复稿件
不得因此引入 21566 App -> Web 自动 fallback
```

'''
    text = replace_once(
        text,
        "## E. 全部验证\n",
        final_submit_check + "## E. 全部验证\n",
        "final submit sync invariants",
    )

    timeout_safety = r'''## 6.5 最终投稿 90 秒超时

文件分片全部上传完成后，最终 create/submit 最多等待 90 秒。

如果超时：

```text
本地任务返回错误
不执行成功 postprocessor
本地录像保留
稿件状态 = 未确认
```

因为 timeout 不能证明远端一定没有处理请求，重新点“小飞机”或重新触发自动投稿前，应先检查 B站创作中心是否已经有对应稿件/BV。

'''
    text = replace_once(
        text,
        "不要在文档或代码里假设这个 fallback 已存在。\n\n---\n\n# 7. 当前明确“不做”的功能",
        "不要在文档或代码里假设这个 fallback 已存在。\n\n" + timeout_safety + "---\n\n# 7. 当前明确“不做”的功能",
        "timeout safety section",
    )

    text = replace_once(
        text,
        "PR #21 2026-08-28  实时日志 WebSocket 25 秒心跳 + 预期断连降噪 + 3 秒自动重连",
        "PR #21 2026-08-28  实时日志 WebSocket 心跳/重连 + 最终投稿 90 秒超时与阶段日志",
        "PR timeline",
    )

    PATH.write_text(text, encoding="utf-8")
    print("CUSTOM_CHANGES.md patched for PR #21")


if __name__ == "__main__":
    main()
