# codex-model-watch

本地监控 Codex 的模型使用、5h/7d 额度水位与容量拒单，并用探针即时验证「请求 A 却被悄悄换成 B」的模型偷换。纯 Python 标准库，数据不出本机。

**中文** | [English](README.en.md)

一个纯本地、零依赖的小工具：监控你 Codex 的**模型使用、额度水位、容量拒单**，并能**主动探测「模型偷换」**—— 你请求的是 A，服务端实际派出的却是 B。

![面板总览](screenshots/panel-overview.png)

## 背景：什么是「模型偷换」

OpenAI 在容量紧张时会启用 safety buffering 机制：你请求高端模型（例如 `gpt-6-astra`），
服务端可能悄悄把请求改派到更快/更小的模型（例如 `gpt-5.6-luna`），**界面不会有任何提示**。
被偷换的输出质量会明显下降，而你完全不知道原因。

本工具把这个现象变成看得见的数据。

## 它能告诉你

| 问题 | 数据来源 | 说明 |
|---|---|---|
| 我都在用哪些模型？用了多少？ | 本地会话日志 | 每一轮实际生效的模型、轮次、token 用量、时长、项目分布 |
| 我的额度还剩多少？ | 本地会话日志 | Codex 上报的 5 小时 / 7 天窗口用量百分比（含重置时间） |
| 我被容量拒单了多少次？ | 本地会话日志 | `Selected model is at capacity` 这类错误的次数与明细 |
| 现在请求 X 会被派什么？ | **主动探针** | 发一条最小请求，读服务端实际派出的模型，即时验证是否被偷换 |
| 历史上偷换过多少次？ | 探针历史 | 每次探针的「请求 → 实际」记录与偷换率 |

## 原理（重要，请先读）

工具有两条独立的数据通道：

### 1. 本地会话日志（零侵入、零额度消耗、全历史）

Codex 会把每一轮会话事件写入本地文件：

```
~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl
```

每个 JSONL 行是一个事件，本工具增量解析其中四类：

| 事件 | 提供什么 |
|---|---|
| `turn_context` | 该轮**实际生效**的模型（`payload.model`）、推理力度、所属项目（cwd） |
| `token_usage_record` | 该轮 token 用量（输入/输出/缓存命中） |
| `event_msg/task_complete` | 轮时长、首字延迟、**错误信息**（容量拒单就在这里） |
| `event_msg/token_count` | `rate_limits` 字段里的 **5h/7d 窗口额度用量百分比** |

> **已知限制（实测结论）**：当服务端偷换模型时，Codex 会把偷换后的模型**一致化地**写进本地日志
> ——「请求字段」和「实际字段」都会变成偷换后的模型。因此**历史日志无法还原偷换事件**，
> 这是探针存在的根本原因。日志里的 `safety_buffering` 事件类型在 Codex 内部存在，
> 但不会被完整落盘。

### 2. 主动探针（偷换的即时验证）

用你本地的 Codex 登录态（`~/.codex/auth.json`），向
`chatgpt.com/backend-api/codex/responses` 发一条最小请求（“hi”，low 推理力度），
读取 SSE 流里 `response.created` 事件返回的模型名：

- 请求 `X`，返回 `X` → 一致
- 请求 `X`，返回别的 → **被偷换**，同时记录响应头 `x-codex-safety-buffering-enabled`

每次探针只消耗极少量额度（一条 "hi"），面板上点一下即可，也可以自己挂 cron 定时跑。

### 3. 可用模型目录（只读）

工具会每 5 分钟读取 `chatgpt.com/backend-api/codex/models`，并从本机 Codex 缓存读取客户端版本。目录请求使用当前 Codex 登录态，只读取账户可用模型，不发送推理请求，也不消耗推理额度。面板每 10 秒更新目录；「刷新模型」按钮可立即请求更新。模型选择框会列出可用模型，也允许手动输入其他名称。自动模式只探测当前目录里最新的 `sol` 和 `astra` 模型，最多两个；也可关闭自动模式并保留手动探针。

目录来自 Codex 内部接口，格式可能变化。请求失败时工具会保留本工具最近一次成功读取的账户目录并显示缓存状态；Codex 自身缓存中的模型只作为未验证候选。切换 Codex 账户会清除旧账户的有效目录。面板会显示目录来源、状态和更新时间。演示模式完全离线，不会读取真实目录或运行真实探针。

OpenAI 的[模型文档](https://developers.openai.com/siwc/token-sharing-open-source/models-and-inference)建议根据实时账户目录选择模型；文档示例使用公开的 `api.openai.com/v1/models`。本工具当前读取 Codex 的 `backend-api` 内部接口，这是本机实测行为，不保证为稳定公开接口。

## 快速开始

要求：Python 3.8+（仅标准库，无需 pip install 任何东西）。Windows / macOS / Linux 均可。

```bash
git clone https://github.com/<you>/codex-model-watch.git
cd codex-model-watch

# 用真实数据启动（自动扫描本地会话日志并打开浏览器）
python codex_model_watch.py

# 没有登录态 / 只想看看界面长什么样
python codex_model_watch.py --demo
```

然后浏览器会自动打开 `http://127.0.0.1:8787`。

![探针与异常](screenshots/panel-probe.png)

### 常用参数

| 参数 | 说明 |
|---|---|
| `--port 8787` | 本地网页端口 |
| `--max-age-days 30` | 只解析最近 N 天的日志，`0` = 全部（首次扫描建议先限定天数，历史越大越慢） |
| `--codex-home PATH` | Codex 主目录（默认 `~/.codex`） |
| `--demo` | 内置演示数据，不读取真实日志 |
| `--scan-only` | 只扫描解析并打印模型分布摘要，不启动网页 |
| `--no-open` | 不自动打开浏览器 |

数据库存在 `~/.codex-model-watch/state.db`（SQLite），重复启动是增量解析，不会重复计数。

### macOS 后台运行

`watch.sh` 将网页进程交给 launchd 管理器监督。它只在你手动启动后运行；不会配置开机或登录自启。plist 保存在 `~/.codex-model-watch/com.codex-model-watch.plist`，不会写入 `~/Library/LaunchAgents`。

```bash
./watch.sh start    # 手动启动并托管
./watch.sh status   # 检查管理器和网页健康状态
./watch.sh restart  # 按当前配置重启并重新载入
./watch.sh stop     # 主动停止，不再自动恢复
./watch.sh          # 手动启动并打开浏览器
```

关闭终端不会停止服务。网页进程退出或连续健康检查失败时，`service_supervisor.py` 会恢复网页进程；launchd 会恢复退出的管理器。运行状态、启动和停止记录写入 `~/.codex-model-watch/watch.log`。注销或重启电脑后，需要再次手动运行 `./watch.sh start`。旧版本遗留的登录自启文件会在手动启动或停止时移入状态目录备份。

## 探针怎么用

1. 先用 Codex 正常登录一次（保证 `~/.codex/auth.json` 存在且未过期）；
2. 打开面板，在「偷换探针」区输入要验证的请求模型（例如 `gpt-6-astra`）；
3. 点「立即探测」，几秒后显示：**请求 X → 实际派出 Y**，一致或被偷换一目了然；
4. 探针历史会沉淀成偷换率曲线，配合定时任务（如每 2 小时一次）即可长期观察偷换窗口。

## 隐私

- 所有解析、统计、存储都发生在本机；数据库在本机；网页只监听 `127.0.0.1`；
- 外发请求包括定时只读模型目录请求，以及你手动触发或启用自动模式后的探针请求；请求发送到你自己的 Codex 后端并使用现有登录态；
- 模型目录请求只读取可用模型，不消耗推理额度；探针会发送一条最小推理请求并消耗少量额度；
- 仓库代码里没有任何凭据、遥测或上报。

## 已知限制

- 历史偷换无法从本地日志还原（见上文「已知限制」），探针只能验证当下；
- rollout 是 Codex 的内部格式，随版本演进可能变化（本工具在 Codex CLI 0.153–0.155 上实测通过）；
- 探针的结论只代表「探测那一刻」的状态，容量紧张时段偷换是动态开关的。

## License

[MIT](LICENSE)
