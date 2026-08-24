# biliup-custom

这是一个尽量薄的 biliup 自定义镜像：跟随官方 `biliup/biliup`，只增强录像路径模板能力，不修改 SQLite 数据库结构、主播配置结构或 B 站上传逻辑。

## 目标目录

推荐的主播文件名模板：

```text
/recordings/{streamer}/{record_date}/[%Y年%m月%d日-%H时%M分%S秒][{streamer}][{title}]
```

例如：

```text
/recordings/测试录播-老姨/2026-08-24/[2026年08月24日-15时35分25秒][测试录播-老姨][六耳猕猴合点宝宝~].flv
```

`{record_date}` 使用 04:00 换日：00:00–03:59 归前一天，04:00 起归当天。文件名里的 `%Y/%m/%d/%H/%M/%S` 始终使用真实录制时间。

例如真实时间是 `2026-08-25 02:10` 时，录像目录仍是 `2026-08-24/`，但文件名仍显示 `2026年08月25日-02时10分...`。

## Docker / 群晖

镜像：

```text
ghcr.io/jijc/biliup-custom:latest
```

推荐挂载：

```yaml
services:
  biliup:
    image: ghcr.io/jijc/biliup-custom:latest
    container_name: biliup
    restart: unless-stopped
    ports:
      - "19159:19159"
    volumes:
      - ./data:/opt
      - /volume1/Biliup:/recordings
    command: server --bind 0.0.0.0 --auth
```

其中：

- `./data:/opt`：保留官方 biliup 的数据库、WebUI 配置和登录状态。
- `/volume1/Biliup:/recordings`：录像最终目录。
- 不需要 `mv` 后处理；`.part` 从创建开始就在最终的主播/日期目录，分段完成后原地变为 `.flv`。
- 45 分钟分段仍在 biliup WebUI 中使用官方 `segment_time` 设置。
- 首轮部署请继续使用默认 `stream-gears` 下载器。04:00 跨日时“新分段进入新日期目录”的行为按 `stream-gears` 的逐段文件生命周期实现并验证；不要在未单独验证前切换到 FFmpeg 内部分段/外部分段并假定行为完全相同。

GitHub Container Registry 的包可见性与仓库可见性相互独立。首次成功发布镜像后，如果 GHCR 把包默认设为 Private，需要在 GitHub 的该 Package 设置里一次性改成 Public；之后群晖即可直接匿名拉取 `ghcr.io/jijc/biliup-custom:latest`，无需保存 GitHub Token。

## 文件名模板兼容策略

只有以 `/recordings/` 开头并包含 `{record_date}` 的模板启用目录模式。其它原有模板继续按官方行为处理，因此普通模板中的 `/` 仍会被清洗成 `_`。

主播名、直播标题和 URL 中的 `/`、`\\`、`:`, `*`, `?`, `"`, `<`, `>`, `|` 等不会变成目录分隔符；动态值会先按文件名规则清洗。路径组件 `.` / `..` 也不会产生目录穿越。

## 自动跟随官方

默认分支上的 GitHub Actions 会：

1. 每 6 小时检查一次 `biliup/biliup` 最新 `master`；定时检查发现 SHA 未变化时不会重复构建。
2. 先运行官方原生能力探测；如果发现可能已经具备等价功能，暂停发布并开 Issue 等待一次人工确认。
3. 对官方源码应用本仓库的薄 patcher，并运行路径相关 Rust 测试。
4. 使用官方 Dockerfile 在原生 Runner 上分别构建 `linux/amd64` 与 `linux/arm64`。
5. 两个架构都成功后，先发布并检查 `ghcr.io/jijc/biliup-custom:upstream-<官方commit短SHA>`，确认 manifest 同时包含 amd64/arm64 后再把同一候选版本晋升为 `ghcr.io/jijc/biliup-custom:latest`。
6. 构建失败时不覆盖现有 `latest`，并创建/更新 GitHub Issue；后续恢复成功会记录恢复信息并关闭失败 Issue。

## 官方未来原生支持时

本项目不打算永久维护一个 fork。如果检测到官方配置/源码出现等价的输出目录或逻辑日期能力，自动同步会暂停发布新 `latest` 并创建提示 Issue，让人确认一次官方参数映射。

确认官方功能满足需求后，可把群晖镜像改回：

```text
ghcr.io/biliup/caution:latest
```

数据库无需迁移。切回官方前，只需把主播的自定义 `/recordings/.../{record_date}/...` 文件名模板改成当时官方支持的模板格式。

## 设计与实施计划

- `docs/superpowers/specs/2026-08-24-biliup-custom-design.md`
- `docs/superpowers/plans/2026-08-24-biliup-custom-implementation.md`
