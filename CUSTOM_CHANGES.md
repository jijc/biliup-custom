# biliup-custom 自定义改动总表与上游同步维护手册

> **这是本仓库最重要的维护文档。**
>
> 目的：以后 `biliup/biliup` 官方仓库发生大规模重构时，不依赖聊天记录或人的记忆，能够快速确认 `biliup-custom` 到底改了什么、为什么改、改到了官方哪些文件、哪些行为绝不能丢，以及同步官方代码后应该怎样验证。
>
> 每增加、删除或改变一个自定义功能，都必须同步更新本文档。

---

## 1. 项目定位

`biliup-custom` 不是一个长期手工维护的完整 fork。

我们的原则是：

1. 每次构建都获取当前官方 `biliup/biliup` 源码。
2. 按固定顺序运行本仓库 `scripts/*.py` 修改器，把少量自定义能力注入官方源码。
3. 修改器必须**严格匹配预期的官方代码结构**；如果官方相关代码发生明显变化，不允许模糊打补丁，而是直接失败并要求人工检查。
4. 修改后的官方源码必须经过 Python 回归测试、针对当前官方源码的 Rust 真编译/测试、官方 Dockerfile + WebUI 完整构建，以及容器 smoke test。
5. 只有验证成功才允许发布 `ghcr.io/jijc/biliup-custom:latest`。
6. 不修改群晖上的数据目录结构和数据库位置；镜像更新应当是可回滚的。

当前群晖长期使用的镜像：

```text
ghcr.io/jijc/biliup-custom:latest
```

群晖关键挂载：

```text
./data:/opt
/volume1/Biliup:/recordings
```

其中：

- `/opt`：biliup 数据库、配置、Cookie 等持久数据。
- `/recordings`：宿主机 `/volume1/Biliup`，所有录像文件的唯一正式根目录。

**禁止为了升级镜像而删除 `/volume1/docker/biliup` 或 `/volume1/Biliup`。**

---

## 2. 当前推荐录像模板与核心语义

推荐主播文件名模板：

```text
/recordings/{streamer}/{record_date}/{daily_seq}-[%Y年%m月%d日-%H时%M分%S秒][{title}]
```

最终文件示例：

```text
/recordings/梦俊/2026-08-27/01-[2026年08月27日-15时32分10秒][直播标题].mp4
/recordings/梦俊/2026-08-27/01-[2026年08月27日-15时32分10秒][直播标题].xml
```

### 2.1 `{record_date}` 逻辑日期

逻辑日以 **04:00** 为边界：

```text
00:00:00 - 03:59:59 -> 归前一天目录
04:00:00 - 23:59:59 -> 归当天目录
```

例如真实录制时间：

```text
2026-08-28 02:10:00
```

则：

```text
目录日期：2026-08-27
文件名真实时间：2026年08月28日-02时10分00秒
```

**目录日期向前偏移 4 小时，但文件名时间不能跟着偏移。**

### 2.2 `{daily_seq}` 每日编号

最终有效录像按主播/逻辑日期目录编号：

```text
01-
02-
03-
...
```

编号只在录像完成过滤、FLV→MP4 成功后最终分配，目的：

- 被碎片过滤删除的文件不占编号；
- 转换失败的片段不占正式 MP4 编号；
- 临时 `.flv.part` 文件名不允许出现字面量 `{daily_seq}`；
- XML 弹幕伴随文件必须和对应视频使用相同编号。

---

## 3. 修改器执行顺序（顺序非常重要）

当前构建/验证链路必须按下面顺序执行：

```text
1.  modify_upstream.py
2.  restore_segment_mp4.py
3.  add_daily_seq_wxpusher.py
4.  fix_daily_seq_temp_filename.py
5.  fix_danmaku_recording_path.py
6.  fix_daily_seq_stream_gears.py
7.  restore_server_log.py
8.  apply_product_customizations.py
9.  fix_override_streamer_fields.py
10. fix_partial_update_safety.py
11. fix_missing_upload_template_safety.py
12. fix_recordings_browser.py
```

对应入口必须保持一致：

- `scripts/apply-and-test.sh`
- `scripts/build-image.sh`
- `.github/workflows/docker-validate.yml`
- `.github/workflows/publish.yml`

### 为什么不能随便改顺序

多个修改器会操作同一个官方文件，尤其：

```text
crates/biliup-cli/src/server/common/upload.rs
crates/biliup-cli/src/server/common/util.rs
crates/biliup-cli/src/server/api/endpoints.rs
```

曾经实际发生过：前一个修改器已经加上“没有投稿模板时保留文件”的安全逻辑，后面的每日编号/WxPusher 修改器又整体替换同一个函数，导致安全逻辑被覆盖。现在通过：

```text
fix_missing_upload_template_safety.py
+ tests/test_modifier_interactions.py
```

在链路后段重新收口，并用交互测试防止再次发生。

**以后增加任何会修改 `upload.rs` / `endpoints.rs` 的 modifier，必须重新检查修改器之间的覆盖关系。**

---

# 4. 自定义功能明细

## 4.1 录像直接写入 `/recordings/<主播>/<逻辑日期>/`

**首次正式方案：PR #6（自动源码修改器架构）**

修改器：

```text
scripts/modify_upstream.py
```

主要修改官方文件：

```text
crates/biliup-cli/src/server/common/util.rs
crates/biliup/src/downloader/util.rs
```

用于检测官方是否已原生支持的只读参考文件：

```text
crates/biliup-cli/src/server/config.rs
```

Marker：

```text
biliup-custom:auto-modifier:v1
```

### 解决的问题

官方原有 `sanitize_filename()` 会把 `/` 当成非法文件名字符替换掉，因此：

```text
/recordings/{streamer}/{record_date}/xxx
```

在官方逻辑下不能作为真正的多级目录模板使用。

### 我们的行为

仅对 `/recordings/` 开头的自定义路径模板保留人为声明的目录分隔符，同时：

- 主播名、标题、URL 里的 `/` 不能创建新目录；
- `\\ / : * ? " < > |`、控制字符等动态内容必须清洗；
- `.`、`..` 等危险路径组件不能造成目录穿越；
- 普通非 `/recordings/` 模板继续保持官方原来的平铺清洗行为；
- `{record_date}` 支持 04:00 逻辑换日；
- stream-gears 时间渲染仍使用真实本地时间。

### 回归测试

```text
tests/test_modify_upstream.py
Rust: biliup_custom_recording_path_tests
Rust: biliup_custom_record_date_tests
```

### 同步官方时重点检查

如果官方以后出现：

```text
recording_output_dir
recording_dir
output_directory
{record_date}
```

或者官方不再把 `/` 当作非法文件名字符，modifier 会返回 `42 / native-review`，必须人工判断官方能力是否已能替代本功能。

---

## 4.2 每个完成 FLV 分段自动无损封装为 MP4

**PR #7**

修改器：

```text
scripts/restore_segment_mp4.py
```

修改官方文件：

```text
crates/biliup-cli/src/server/common/upload.rs
```

Marker：

```text
biliup-custom:auto-mp4:v1
```

### 目标

录制活动中：

```text
xxx.flv.part
```

分段完成：

```text
xxx.flv
```

随后立即执行 ffmpeg 无重编码 remux：

```text
-c copy
```

得到：

```text
xxx.mp4
```

### 数据安全规则

1. `.part` 永远不能进入转换。
2. ffmpeg 先写临时输出，例如 `.mp4.partial`。
3. ffmpeg 成功且输出文件非空后，才 rename 成正式 `.mp4`。
4. 只有正式 MP4 已验证有效后，才允许删除源 FLV。
5. 转换失败必须删除失败的临时输出并保留原 FLV。
6. 已有非空 MP4 时允许复用，保证重试幂等。

### 当前额外职责

PR #16 后，这个修改器还参与“无投稿模板”的安全分支：

- 显式 `Noop`：仍执行用户主动配置的后处理；
- `upload_streamers_id` 意外为空：仍完成 MP4 转换，但禁止执行可能含 `rm` 的后处理。

### 测试

```text
tests/test_segment_mp4_modifier.py
Rust: biliup_custom_auto_mp4_tests
```

---

## 4.3 每日 01/02/03 编号 + WxPusher 通知

**PR #10**

修改器：

```text
scripts/add_daily_seq_wxpusher.py
```

修改/创建官方文件：

```text
crates/biliup-cli/src/server/common/mod.rs
crates/biliup-cli/src/server/common/download.rs
crates/biliup-cli/src/server/common/upload.rs
crates/biliup-cli/src/server/common/wxpusher.rs   # 由 modifier 创建
```

Marker：

```text
biliup-custom:daily-seq-wxpusher:v1
```

### 每日编号

设计要求：

- 以 `/recordings/<主播>/<record_date>/` 为扫描目录；
- 查找已有 `NN-` 文件，取最大编号 + 1；
- 视频及伴随文件使用同一编号；
- 编号发生在 MP4 转换之后、自动上传之前。

### WxPusher

通过环境变量启用：

```text
WXPUSHER_APP_TOKEN
WXPUSHER_UIDS
```

通知是 best-effort，失败绝不能影响录制、转换或投稿。

当前通知事件：

- 主播开播；
- 确认停播；
- FLV→MP4 转换失败；
- 单个上传阶段错误；
- B站稿件提交成功；
- B站稿件提交失败。

### 测试

```text
tests/test_daily_seq_wxpusher_modifier.py
Rust: biliup_custom_daily_sequence_tests
Rust: biliup_custom_wxpusher_tests
```

---

## 4.4 修复 `{daily_seq}` 泄漏到临时录像名

**PR #11**

修改器：

```text
scripts/fix_daily_seq_temp_filename.py
```

修改官方文件：

```text
crates/biliup-cli/src/server/common/util.rs
crates/biliup-cli/src/server/common/upload.rs
```

Marker：

```text
biliup-custom:daily-seq-temp-clean:v1
```

### 原问题

如果把 `{daily_seq}` 直接写入文件模板，活动录像可能出现：

```text
{daily_seq}-xxx.flv.part
```

这既难看，又会导致后续编号逻辑混乱。

### 当前策略

录制时先移除 `{daily_seq}` 占位符：

```text
xxx.flv.part
```

录像完成并经过过滤/转换后，最终 rename 为：

```text
01-xxx.mp4
```

XML 等 companion 文件一起 rename。

### 测试

```text
tests/test_daily_seq_wxpusher_modifier.py
tests/test_daily_seq_stream_gears_modifier.py
Rust: biliup_custom_daily_seq_temp_tests
```

---

## 4.5 StreamGears 实际入口再次清除 `{daily_seq}`

**PR #12**

修改器：

```text
scripts/fix_daily_seq_stream_gears.py
```

修改官方文件：

```text
crates/biliup-cli/src/server/common/util.rs
```

检查但不直接修改：

```text
crates/biliup-cli/src/server/core/downloader/stream_gears.rs
```

Marker：

```text
biliup-custom:daily-seq-stream-gears:v1
```

### 原因

真实默认下载器 StreamGears 创建活动文件时走：

```text
download_config.recorder.filename_template()
```

仅在 `generate_filename()` / `format_filename()` 清理占位符并不够，因此必须确保 `Recorder::filename_template()` 自身也不会把 `{daily_seq}` 传给 StreamGears。

### 回归要求

只要官方 StreamGears 不再走该入口，modifier 必须停止并要求人工复核，而不是继续假设行为一致。

---

## 4.6 弹幕 XML 从创建第一秒就写入正确录像目录

**PR #15**

修改器：

```text
scripts/fix_danmaku_recording_path.py
```

修改官方文件：

```text
crates/biliup-cli/src/server/common/util.rs
crates/danmaku/src/client.rs
```

Marker：

```text
biliup-custom:danmaku-recording-path:v1
```

### 原问题

官方弹幕文件使用独立的 `danmaku_filename_template()`，会按照传统文件名逻辑把目录 `/` 清洗掉，也不知道 `{record_date}` 的 04:00 换日。

结果可能先在程序当前目录产生类似错误的 XML，等视频分段完成时再 rolling 到视频旁边。若 NAS/容器在 rolling 前异常退出，就可能留下孤立 XML。

### 当前行为

开启平台弹幕录制后：

```text
/recordings/主播/逻辑日期/xxx.xml
```

从创建第一秒就正确落盘。

同时：

- 03:59:59 -> 前一逻辑日；
- 04:00:00 -> 当天逻辑日；
- XML 初始名不保留 `{daily_seq}`；
- 分段 rolling 后继续和对应视频同名；
- 最终 daily sequence 会让视频/XML 一起变成 `01-...`。

### 非目标

我们**没有**把弹幕烧进视频画面；也**没有**自动把 XML 上传成 B站播放器弹幕。

XML 目前作为本地数据保存，适合后续给 AI 做热点/弹幕峰值分析。

### 测试

```text
tests/test_danmaku_recording_path_modifier.py
Rust(danmaku): biliup_custom_danmaku_path_tests
```

---

## 4.7 恢复 WebUI 主程序日志 `ds_update.log`

**PR #9**

修改器：

```text
scripts/restore_server_log.py
```

修改官方文件：

```text
crates/stream-gears/src/server.rs
```

Marker：

```text
biliup-custom:server-log:v1
```

### 解决的问题

Docker/Python 实际 server 入口走 `stream_gears`，WebUI 却需要读取主程序日志 `ds_update.log`。

当前做法：

- 保留官方 `download.log`；
- 额外增加 `ds_update.log` tracing writer；
- 两套日志并存。

如果官方以后原生在这个入口写 `ds_update.log`，modifier 会触发 native review，避免重复写两份。

---

## 4.8 暂停监听状态持久化

**PR #8**

修改器：

```text
scripts/apply_product_customizations.py
```

修改官方文件：

```text
crates/biliup-cli/src/server/infrastructure/repositories.rs
crates/biliup-cli/src/server/api/endpoints.rs
crates/biliup-cli/src/lib.rs
crates/biliup-cli/src/server/core/monitor.rs
```

Marker：

```text
biliup-custom:persistent-pause:v1
```

### 设计

不改官方 `livestreamers` 表结构，不增加自定义数据库 migration。

复用官方通用 `configuration` 表：

```text
key   = biliup-custom:paused-streamer
value = <streamer id>
```

### 行为

- 点暂停：先持久化，再把 Worker 置为 Pause；
- 恢复：删除持久化状态；
- 重启 biliup：从数据库恢复 Pause；
- 暂停主播仍显示在 UI 中，但不会进入主动轮询队列；
- 编辑主播导致 Worker 重建时，Pause 仍然保持；
- 删除主播时同步清理暂停记录，防止 SQLite 以后复用 id 造成错误暂停。

---

## 4.9 UI：历史列宽、任务平台高度、主播状态角标

修改器：

```text
scripts/apply_product_customizations.py
```

### 直播历史列宽

官方文件：

```text
app/(app)/job/page.tsx
```

Marker：

```text
biliup-custom:live-history-layout:v1
```

固定：

```text
名称 180
标题 360
封面 120
```

### 任务平台全高度

官方文件：

```text
app/(app)/status/page.tsx
```

Marker：

```text
biliup-custom:task-platform-height:v1
```

关键样式：

```text
main height: 100%
.semi-layout-content > main > ul { height: 100%; box-sizing: border-box; }
```

### 录播管理状态角标

**PR #9**

官方文件：

```text
app/(app)/streamers/page.tsx
```

Marker：

```text
biliup-custom:streamer-status-tags:v1
```

当前配色/文案：

```text
Working -> 红色 录制中
Idle    -> 蓝色 空闲
Pending -> 绿色 检测中
Pause   -> 灰色 暂停中
```

这里只改视觉，不改变监听/状态机语义。

---

## 4.10 `/recordings` 统一录像浏览、历史播放、手动投稿选择

**PR #14**（PR #13 为中间验证 PR，最终未合并）

修改器：

```text
scripts/fix_recordings_browser.py
```

修改官方文件：

```text
crates/biliup-cli/src/server/api/endpoints.rs
crates/biliup-cli/src/server/router.rs
app/(app)/history/page.tsx
app/(app)/upload-manager/page.tsx
```

Marker：

```text
biliup-custom:recordings-browser:v1
biliup-custom:recordings-static:v1
biliup-custom:recordings-history:v1
biliup-custom:recordings-upload-picker:v1
```

### 原问题

官方 `/v1/videos` 和静态播放逻辑假设录像都在程序当前目录 `.`，而我们已经把录像变成：

```text
/recordings/<主播>/<日期>/...
```

导致：

- 投稿管理文件选择看不到真实录像；
- 历史记录不完整；
- 多级目录视频不能正常播放；
- 根目录旧文件反而可能混入列表。

### 当前行为

`GET /v1/videos`：

- 从 `/recordings` 递归扫描；
- 返回相对路径，如 `梦俊/2026-08-27/01-xxx.mp4`；
- 支持 mp4/flv/3gp/webm/mkv/ts；
- `.part` 和非媒体文件排除；
- 不跟随目录 symlink；
- 按相对路径排序，即主播 -> 日期 -> 文件名。

投稿管理：

- 文件选择是**全局本地录像池**，不是某个主播自己的文件列表；
- 只要本地媒体文件还存在，就会显示；
- “已经上传过”本身不会让它从列表消失；只有文件被删除/移动才消失。

静态播放/手动投稿路径安全：

- `/static/{*path}` 支持多级路径；
- 禁止绝对路径；
- 禁止 `..`、`.`、反斜杠穿越；
- 禁止不支持的扩展名；
- canonicalize 后必须仍位于 `/recordings` 内；
- symlink 指向根目录外必须拒绝。

历史播放 URL：

- 对路径**每个 segment 单独 `encodeURIComponent`**；
- 然后重新用 `/` 拼接；
- 兼容中文、空格、`#` 等 URL 保留字符，同时不破坏目录层级。

### 测试

```text
tests/test_recordings_browser_modifier.py
Rust: biliup_custom_recordings_path_tests
```

---

## 4.11 配置覆写导致“投稿模板/文件名模板被清空”数据安全修复

**PR #16**

这是目前最高优先级的数据安全修复之一。

修改器：

```text
scripts/fix_override_streamer_fields.py
```

修改官方文件：

```text
app/ui/OverrideModal.tsx
app/lib/api-streamer.ts
app/(app)/upload-manager/edit/page.tsx
```

Markers：

```text
biliup-custom:preserve-streamer-fields:v1
biliup-custom:live-streamer-schema:v1
biliup-custom:upload-template-bool-roundtrip:v1
```

### 事故原因

官方后端真实主播字段：

```text
filename_prefix
upload_streamers_id
```

但 WebUI `OverrideModal` 的旧白名单仍使用：

```text
filename
upload_id
```

还包含过期字段：

```text
split_time
split_size
```

当用户只想通过“扳手 / 配置覆写”修改画质、弹幕等单主播配置时，前端可能把真实的：

```text
filename_prefix
upload_streamers_id
```

当成未知字段丢掉，然后整对象 PUT 回后端。

后果：

```text
文件名模板 -> NULL
投稿模板   -> NULL
```

下一次 Worker 重建后，该主播就没有投稿模板。

如果同时有 `postprocessor = rm`，旧逻辑会把录像当“不需要上传”的任务执行后处理，最终造成：

```text
B站没稿件 + NAS 本地录像也被删
```

### 前端修复

`OverrideModal`：

```text
filename    -> filename_prefix
upload_id   -> upload_streamers_id
```

移除过期的：

```text
split_time
split_size
```

把 `upload_status` / `statusTag` 明确当作 UI/响应字段，不允许污染 override 配置。

最关键的未来兼容保险：

```text
await onOk({ ...entity, ...cleanValues })
```

即配置覆写弹窗只修改它知道的字段，未触碰的主播顶层字段从原始 `entity` 继承，防止以后官方新增字段而弹窗没同步时再次被“漏传清空”。

### TypeScript 模型修复

`LiveStreamerEntity` 对齐真实后端字段：

```text
filename_prefix?: string | null
upload_streamers_id?: number | null
```

### 投稿模板布尔字段 round-trip 修复

官方 GET 模型把：

```text
up_selection_reply
up_close_reply
up_close_danmu
```

序列化为 bool；当前创建/更新 API 又仍兼容 0/1。

WebUI 类型改为：

```text
boolean | number
```

编辑已有模板时使用：

```text
Boolean(data.xxx)
```

避免已有 `true` 因 `true === 1` 为 false 而在编辑保存后被悄悄关闭。

### 测试

```text
tests/test_config_safety_customizations.py
```

---

## 4.12 后端真正的“部分更新”保护

**PR #16 审计后新增的第二层保险**

修改器：

```text
scripts/fix_partial_update_safety.py
```

修改官方文件：

```text
crates/biliup-cli/src/server/api/endpoints.rs
```

Marker：

```text
biliup-custom:partial-update-safety:v1
```

### 为什么还要后端保护

只修前端字段名仍不够。

官方多个 PUT/更新接口使用“收到整个对象后 `update_all_fields`”的模式：客户端漏一个 optional 字段，就可能把数据库原值覆盖为 NULL。

这类风险存在于：

```text
主播配置
空间全局配置
B站投稿模板
```

### 当前 Patch 语义

客户端只提交它想改的字段：

```text
没出现在 JSON 里的字段 -> 保留数据库/当前配置原值
明确出现在 JSON 且值为 null -> 用户主动清空
```

实现方式：

1. 读取当前保存对象；
2. 转为 JSON object；
3. 只用请求 JSON 中存在的 key 覆盖；
4. 再反序列化为正式模型；
5. 最后执行原数据库更新。

上传模板的 0/1 布尔兼容会先规范化为 true/false，再合并。

### 覆盖接口

```text
PUT /v1/streamers
PUT /v1/configuration
投稿模板新增/更新 endpoint 中的更新分支
```

### 测试

```text
tests/test_partial_update_safety_modifier.py
```

### 重要维护原则

以后如果官方自己改成 PATCH DTO、`Option<Option<T>>`、字段级 SQL UPDATE 或等价的 partial update 语义，应该考虑删除这个 modifier，而不是叠加两套 patch 逻辑。

---

## 4.13 投稿模板意外丢失时绝不执行破坏性后处理

**PR #16**

修改器：

```text
scripts/fix_missing_upload_template_safety.py
```

同时 `restore_segment_mp4.py` 也包含同一安全语义，最后再由该 modifier 收口。

修改官方文件：

```text
crates/biliup-cli/src/server/common/upload.rs
```

Marker：

```text
biliup-custom:preserve-files-without-upload-template:v1
```

### 必须区分的两种情况

#### A. 用户明确选择 Noop 投稿模板

这是用户主动表示：

```text
不上传，但仍按自己的 postprocessor 执行
```

因此显式 Noop 继续允许：

```text
run / mv / rm
```

等正常后处理。

#### B. `upload_streamers_id` 为空 / 没有任何投稿模板

这可能是配置损坏或 UI Bug，而不是用户主动要求不上传。

安全策略：

1. 正常收完分段；
2. FLV 仍按我们的规则转 MP4；
3. 本地文件保留；
4. **跳过整个 postprocessor**，尤其禁止 `rm`；
5. 输出明确 warning：

```text
No upload template is bound; preserving local recording files and skipping postprocessor
```

### 目标

以后即使配置再次异常，最坏结果也只能是：

```text
没有自动上传，但录像还在 NAS
```

绝不能再出现：

```text
没有上传 + 本地也删除
```

### 测试

```text
tests/test_config_safety_customizations.py
tests/test_segment_mp4_modifier.py
tests/test_modifier_interactions.py
```

---

# 5. `filtering_threshold`：我们明确保持官方行为

**这一项当前故意不改。**

当前部署策略是过滤掉 **70MB 以下**的小录像，因此 `filtering_threshold` 应继续按实际配置保留，而不是强制改成 0。

官方处理链路中，文件完成分段后会先做 FileValidator 检查；小于阈值时会直接删除物理文件，然后返回“文件太小”的错误，该文件不会进入上传队列。

因此如果配置：

```text
filtering_threshold = 70 MB
```

行为就是：

```text
< 70MB  -> 直接删除，不上传
>=70MB  -> 进入后续 MP4 / 编号 / 上传流程
```

### 这是“用户主动过滤”而不是“事故保护”

PR #16 的“没有投稿模板时保留文件”**不应绕过 filtering_threshold**。

否则会改变明确要求的 70MB 过滤语义，并导致大量很小的片段进入 B站投稿流程。

### 风险必须知道

如果一场真正有价值的直播因为很短、断流或主播马上下播，最终单文件不足 70MB，它仍会按配置被永久过滤掉。

这是当前接受的产品取舍。

---

# 6. B站自动投稿与本地文件删除的安全边界

## 6.1 正常成功上传

当前预期顺序：

```text
录像完成
-> filtering_threshold 检查
-> FLV→MP4
-> daily_seq
-> 上传各 P
-> B站最终 create/submit
-> Submit successful
-> postprocessor
-> 如果 postprocessor 有 rm，再删除本地文件
```

## 6.2 单文件上传失败

失败文件不能被计入成功上传路径列表，不能因为失败继续当成功流程删除。

## 6.3 B站最终投稿失败，例如 21566

`submit_to_bilibili()` 返回错误后必须直接结束该任务，不执行成功后的 postprocessor。

因此正常的 21566 风控失败应保留本地媒体文件。

## 6.4 当前 B站 21566 策略

已知错误：

```text
code: 21566
message: 投稿过于频繁，建议将APP升级至最新版本后再试
```

目前全局 `submit_api` 已可选择：

```text
web
```

但我们**尚未实现 App 失败 21566 后自动 fallback 到 Web**。

不要在文档或代码里假设这个 fallback 已存在。

---

# 7. 当前明确“不做”的功能

为了防止以后误以为已经实现：

1. **没有弹幕烧录视频**：XML 不会被渲染进 MP4 画面。
2. **没有把 XML 自动上传为 B站播放器弹幕**。
3. **没有 21566 自动 App→Web fallback**。
4. **投稿管理文件选择没有按主播自动过滤**；它是 `/recordings` 的全局媒体池。
5. **不会根据“是否曾投稿过”隐藏本地文件**；只看文件是否实际存在。
6. **没有取消 `filtering_threshold` 的删除行为**。
7. **没有修改明确配置的 `postprocessor=rm` 成功后删除语义**。

---

# 8. 关键 Marker 总表

以后同步大版本时，可直接在修改后的 upstream checkout 全局搜索：

```text
biliup-custom:auto-modifier:v1
biliup-custom:auto-mp4:v1
biliup-custom:daily-seq-wxpusher:v1
biliup-custom:daily-seq-temp-clean:v1
biliup-custom:danmaku-recording-path:v1
biliup-custom:daily-seq-stream-gears:v1
biliup-custom:server-log:v1
biliup-custom:persistent-pause:v1
biliup-custom:live-history-layout:v1
biliup-custom:task-platform-height:v1
biliup-custom:streamer-status-tags:v1
biliup-custom:preserve-streamer-fields:v1
biliup-custom:live-streamer-schema:v1
biliup-custom:upload-template-bool-roundtrip:v1
biliup-custom:partial-update-safety:v1
biliup-custom:preserve-files-without-upload-template:v1
biliup-custom:recordings-browser:v1
biliup-custom:recordings-static:v1
biliup-custom:recordings-history:v1
biliup-custom:recordings-upload-picker:v1
```

缺任意一个都要确认：

- 是 modifier 没执行？
- 是后面的 modifier 覆盖了前面的代码？
- 还是官方已经原生实现了等价功能，需要删除我们的自定义？

---

# 9. 当前官方源码受影响文件总表

下面不是说本仓库直接保存这些官方文件，而是构建时 modifier 会修改从 `biliup/biliup` 拉下来的 checkout。

## Rust / 后端

```text
crates/biliup-cli/src/server/common/util.rs
crates/biliup/src/downloader/util.rs
crates/biliup-cli/src/server/common/upload.rs
crates/biliup-cli/src/server/common/download.rs
crates/biliup-cli/src/server/common/mod.rs
crates/biliup-cli/src/server/common/wxpusher.rs          # customizer 创建
crates/danmaku/src/client.rs
crates/stream-gears/src/server.rs
crates/biliup-cli/src/server/infrastructure/repositories.rs
crates/biliup-cli/src/server/api/endpoints.rs
crates/biliup-cli/src/lib.rs
crates/biliup-cli/src/server/core/monitor.rs
crates/biliup-cli/src/server/router.rs
```

## WebUI

```text
app/(app)/job/page.tsx
app/(app)/status/page.tsx
app/(app)/streamers/page.tsx
app/(app)/history/page.tsx
app/(app)/upload-manager/page.tsx
app/(app)/upload-manager/edit/page.tsx
app/ui/OverrideModal.tsx
app/lib/api-streamer.ts
```

## 仅用于结构检查/guard 的官方文件

```text
crates/biliup-cli/src/server/config.rs
crates/biliup-cli/src/server/core/downloader/stream_gears.rs
```

---

# 10. 本仓库测试文件与责任

```text
tests/test_modify_upstream.py
  -> /recordings 多级目录、04:00 record_date、路径清洗

tests/test_segment_mp4_modifier.py
  -> FLV→MP4、.part 排除、无投稿模板分支

tests/test_daily_seq_wxpusher_modifier.py
  -> daily_seq 时机、临时 `{daily_seq}` 清理、WxPusher 事件

tests/test_daily_seq_stream_gears_modifier.py
  -> 默认 StreamGears 实际 filename_template 入口与占位符清理

tests/test_danmaku_recording_path_modifier.py
  -> XML 同目录、04:00 换日、初始名清理

tests/test_server_log_modifier.py
  -> download.log 保留 + ds_update.log 恢复

tests/test_product_customizations.py
  -> Pause 持久化、UI 列宽/高度/状态角标

tests/test_recordings_browser_modifier.py
  -> /recordings 递归列表、历史播放、手动投稿、路径安全

tests/test_config_safety_customizations.py
  -> OverrideModal 字段、前端 schema、投稿模板 bool round-trip、无模板保文件

tests/test_partial_update_safety_modifier.py
  -> 主播/空间配置/投稿模板 partial update 语义

tests/test_modifier_interactions.py
  -> 多 modifier 连续执行后，关键数据安全逻辑不能被覆盖掉
```

**新增 modifier 时至少必须有：**

- 单元 fixture 测试；
- idempotent 测试；
- 如果会修改其他 modifier 已修改的同一个函数，必须增加 interaction test。

---

# 11. CI / 发布流程

## 11.1 PR 验证

### `.github/workflows/validate.yml`

负责：

```text
Python modifier tests
+ 拉取当前官方 master
+ 应用全部 modifier
+ 针对当前官方源码跑 focused Rust tests
```

### `.github/workflows/docker-validate.yml`

负责：

```text
拉取当前官方 master
+ 应用全部 modifier
+ 使用官方 Dockerfile 构建完整镜像
+ WebUI Next.js production build / TypeScript 检查
+ Rust release build
+ docker run --help smoke test
```

### 发布前最低条件

两套 workflow 都必须：

```text
status = completed
conclusion = success
```

不能因为 Python 测试绿了就跳过完整 Docker/WebUI。

---

## 11.2 `latest` 发布

`.github/workflows/publish.yml`

推送到 `main` 后会构建：

```text
linux/amd64
linux/arm64
```

分别 push digest 后，再合成 multi-arch manifest：

```text
ghcr.io/jijc/biliup-custom:latest
ghcr.io/jijc/biliup-custom:upstream-<official-short-sha>
ghcr.io/jijc/biliup-custom:sha-<custom-main-short-sha>
```

最终还必须执行：

```text
docker buildx imagetools inspect latest
docker pull latest
docker run --rm latest --help
```

只有最后 smoke 成功才能告诉 NAS 用户升级。

---

# 12. 上游大版本同步检查清单

当官方 biliup 有明显重构时，不要直接“把冲突修到能编译”为止。按下面顺序检查。

## A. 先记录官方 SHA

```bash
git ls-remote https://github.com/biliup/biliup.git refs/heads/master
```

记录此次同步的官方 SHA。

## B. 逐个运行 modifier

必须保持第 3 节顺序。

如果任何脚本：

```text
return code 42
native-review
```

立即停止自动发布。

## C. 检查是否官方已经原生解决

不要机械地“修 anchor”。先看官方是否已经提供等价或更好的：

- 录像输出目录；
- 路径模板；
- 逻辑日期；
- MP4 remux；
- 弹幕输出路径；
- Pause 持久化；
- `/recordings` 浏览；
- partial update；
- 无模板文件安全保护。

如果官方已经原生实现，应优先删除我们的 modifier，而不是维护重复实现。

## D. 核对关键不变量

### 文件安全

```text
转换失败保留 FLV
投稿失败不 rm
投稿模板缺失不 rm
显式 Noop 才按用户 postprocessor 执行
filtering_threshold 继续按用户设置过滤
```

### 路径

```text
/recordings/<主播>/<逻辑日>/
04:00 换日
真实时间不偏移
动态元数据不能目录穿越
```

### 编号

```text
活动文件不出现 {daily_seq}
过滤/转换后才编号
视频/XML 同编号
```

### 配置

```text
配置覆写不能清 filename_prefix
配置覆写不能清 upload_streamers_id
漏传 JSON 字段必须保留原值
显式 null 才允许清空
```

## E. 全部验证

至少：

```text
python -m unittest discover -s tests -v
focused Rust tests
完整官方 Dockerfile
完整 WebUI build
容器 --help smoke
amd64 + arm64 publish
published latest pull + smoke
```

---

# 13. 关键事故复盘：为什么这份文档必须长期维护

2026-08-27 发生过一次严重数据丢失事故：

1. 给主播做单独“配置覆写”（例如弹幕/画质等）。
2. WebUI 旧字段白名单把 `filename_prefix` / `upload_streamers_id` 漏掉。
3. 后端使用整对象 `update_all_fields`，数据库字段被写成 NULL。
4. 主播 Worker 重建后没有投稿模板。
5. biliup 将其视为“无上传配置”任务。
6. 录像完成、转 MP4 后仍执行 postprocessor。
7. 配置中包含 `rm`，导致本地 MP4 被删除。
8. 因为根本没进入投稿流程，B站也没有稿件。

最终结果：

```text
B站没有 + NAS 录像也没有
```

PR #16 的三个安全层就是为阻止这类事故再次发生：

```text
前端字段/原 entity 合并
+ 后端 partial update
+ 无投稿模板时跳过 destructive postprocessor
```

以后任何重构都不能只看“页面能保存、代码能编译”，必须专门验证这三层。

---

# 14. 重要 PR 时间线

下面只列真正影响当前运行版本的主线 PR；中间实验 PR 不作为功能来源。

```text
PR #6  2026-08-24  自动源码 modifier + 当前官方源码验证 + 发布架构
PR #7  2026-08-25  完成 FLV 分段自动安全 remux MP4
PR #8  2026-08-25  Pause 持久化 + 历史列宽 + 任务平台高度
PR #9  2026-08-25  无上传任务也走 MP4 + ds_update.log + 状态角标
PR #10 2026-08-25  每日编号 + WxPusher
PR #11 2026-08-25  清除临时文件 `{daily_seq}`
PR #12 2026-08-25  StreamGears 实际入口补 daily_seq 清理
PR #14 2026-08-26  /recordings 递归浏览 + 投稿选择 + 历史播放 + 路径安全
PR #15 2026-08-27  弹幕 XML 同目录 + 04:00 逻辑日期
PR #16 2026-08-27  投稿/文件名模板丢失修复 + partial update + 无模板保文件
```

已知主线关键 merge commit（可用于事故追踪，不能代替本文档）：

```text
PR #7  -> 47ddfd163ca4371b8f73bc4bdb820a7e92546203
PR #8  -> 6fb99e38c1314b01b0d5e48e0b03d6f775ad3f52
PR #14 -> d93fe096985fe462cf88406e0350d13e13fb663d
PR #15 -> 4f95e692c55840275a3f8cb30cdbac44c0f1a9f6
```

如果本文档与当前代码冲突：**以代码和测试为最终事实，并立即修正文档。**

---

# 15. NAS 更新与回滚原则

更新：

```bash
docker compose pull
docker compose up -d
```

但只允许在 GitHub `latest` 的最终发布 smoke 成功后执行。

数据目录保持：

```text
/volume1/docker/biliup/data
/volume1/Biliup
```

镜像更新不能删除这些目录。

如果新 `latest` 出现运行级问题，应优先回退到上一个已经验证过的镜像 digest/tag，而不是删除数据库重装。

---

# 16. 每次新增自定义功能时必须更新本文档的内容

新增功能 PR 合并前，请在 `CUSTOM_CHANGES.md` 至少补：

1. 功能名称、PR 编号、日期；
2. 为什么要改；
3. modifier 文件；
4. 会修改哪些官方 upstream 文件；
5. Marker；
6. 核心行为和数据安全规则；
7. 非目标/明确没有实现的内容；
8. 对应测试；
9. 与其他 modifier 的执行顺序依赖；
10. 官方以后原生支持时怎样判断是否可以删除自定义实现。

**不要只写“修复某某 BUG”一句话。** 未来维护者必须能够仅通过这个文件还原我们的设计意图。

---

最后更新：2026-08-27

当前文档对应自定义分支：`fix/preserve-upload-template-and-local-files`（PR #16，合并后以 `main` 为准）。
