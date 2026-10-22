# DeeperSeeker（中文文档）

[![CI Pipeline](https://github.com/AmanCode22/deeperseeker/actions/workflows/tests.yaml/badge.svg)](https://github.com/AmanCode22/deeperseeker/actions/workflows/tests.yaml)
[![Release image](https://github.com/AmanCode22/deeperseeker/actions/workflows/release.yml/badge.svg)](https://github.com/AmanCode22/deeperseeker/actions/workflows/release.yml)

自托管的 DeepSeek 反向代理：把 DeepSeek 网页会话变成 **OpenAI / Anthropic 兼容 API**——底层是 FastAPI，支持流式输出、工具调用、文件与视觉。

[English](README.md) · **简体中文**

## 目录

- [这是什么？](#这是什么)
- [警告——运行前必读](#警告运行前必读)
- [状态](#状态)
- [快速开始](#快速开始)
- [认证令牌配置](#认证令牌配置)
- [配置（`.env`）](#配置env)
- [API 端点与用法](#api-端点与用法)
- [官方 API 模式（退路）](#官方-api-模式退路)
- [功能特性](#功能特性)
- [安全默认值](#安全默认值)
- [故障排查](#故障排查)
- [价格](#价格)
- [项目文档](#项目文档)
- [免责声明](#免责声明)

## 这是什么？

DeeperSeeker 运行在你自己的机器上，保管你自己的 DeepSeek 网页会话令牌，并对外暴露标准 API 端点（`/v1/chat/completions`、`/v1/responses`、`/v1/messages`），替你与 DeepSeek 网页后端通信。任何说 OpenAI 或 Anthropic 协议的客户端——Claude Desktop、Agent 框架、脚本——都可以直接指向它，配套的仪表盘负责令牌池与用量统计。唯一的默认模型是 `v4.1flash`。

解决 DeepSeek PoW 挑战所用的 wasm 文件来自我的另一个仓库：<https://github.com/AmanCode22/deepseek_pow_solver/>。如果这个仓库对你有用，请给它和 solver 都点个 Star。

想在 Claude Desktop 桌面应用里使用 DeeperSeeker？请看 [Claude Desktop 配置指南](CLAUDE_DESKTOP_SETUP.md)。

## 警告——运行前必读

- 本项目驱动的是**逆向工程得到的网页接口**，不是官方认可的 API。自动化使用违反 DeepSeek 服务条款。
- **封号风险真实存在，后果自负**：请只用专门注册的小号，绝不要用个人主力账号。账号可能随时、无预警地被封。
- 仅限个人自用。自己跑、自己的账号、风险自己承担。

**永远不要搭建公共转发站。** 这是一个面向单人自有账号的自托管工具。在它之上运营共享/公共转发站，恰恰会集中那些导致封号和项目被清理的滥用行为（参考 ds2api 的下场）——此类用法不受支持，相关求助 issue 会被直接关闭。

**下架 / 联系政策。** 我是 Aman（AmanCode22，amanady125@gmail.com）。如果我是 DeepSeek 且本项目对你造成损害：给我发邮件，我会回应——下架整个仓库或移除特定组件都在可谈范围内，通知会被尽快处理。漏洞与安全问题请遵循 [SECURITY.md](SECURITY.md)。

## 状态

| 检查项 | 状态 | 最近验证 | 备注 |
|---|---|---|---|
| 上游健康（桥接模式） | 见 `/health?deep=1` | 自己跑一次 | 深度探活会真实执行一次 PoW + 聊天会话往返；发布时写进表格的结论发布即过时，唯一的可信来源是带认证的探活 |
| PoW（DeepSeekHashV1） | 经 [deepseek_pow_solver](https://github.com/AmanCode22/deepseek_pow_solver/) wasm 正常工作 | 2026-10 | 若 DeepSeek 提高难度，solver 仓库先收到修复 |
| OpenAI 面（`/v1/chat/completions`、`/v1/responses`） | 正常 | 2026-10 | 流式 + 工具；见客户端矩阵 |
| Anthropic 面（`/v1/messages`） | 正常 | 2026-10 | Claude Desktop 指南见 CLAUDE_DESKTOP_SETUP.md |
| 官方 API 模式 | 见[官方 API 模式](#官方-api-模式退路) | 2026-10 | 桥接挂掉时的退路 |

一张你不再去看的状态表就是一张说谎的表——把"最近验证"当作该结论的保质期。

逐客户端的兼容情况见 [docs/CLIENT_MATRIX.md](docs/CLIENT_MATRIX.md)：一次真实、带日期的运行对应一行——没跑过，就没有这一行。

## 快速开始

四种运行方式，读的都是同一份 `.env`。无论选哪种，第一步都是：

```bash
cp .env.example .env
```

至少在该文件里设置 `DEEPSEEKER_ADMIN_PASSWORD`——[安全默认值](#安全默认值)会拒绝"默认凭据 + 对外暴露"的组合启动。

### 方式一——预构建镜像（ghcr.io，amd64 + arm64，推荐）

镜像**只由 CI 构建**（`.github/workflows/release.yml`）——不在本地构建，也不手动构建。版本只有一个来源：`pyproject.toml`（当前为 `version = "6.0"`）。发布流程：改动该字段，推送对应的 `v<版本>` 标签（此处即 `v6.0`）——若标签与 `pyproject.toml` 不一致，工作流会拒绝构建，一致则发布多架构（amd64 + arm64）镜像。拉取即用，无需克隆仓库：

```bash
export DEEPSEEKER_ADMIN_PASSWORD='设置一个真实密码'   # P0-8 拒绝在对外暴露时使用 admin/admin
docker run -d --name deeperseeker -p 4000:4000 \
  -e DEEPSEEKER_ADMIN_USER=admin -e DEEPSEEKER_ADMIN_PASSWORD \
  ghcr.io/amancode22/deeperseeker:latest
```

更推荐固定到你验证过的 digest，而不是跟随可变标签：

```bash
docker pull ghcr.io/amancode22/deeperseeker:6.0 \
  && docker inspect --format '{{index .RepoDigests 0}}' ghcr.io/amancode22/deeperseeker:6.0
# 然后运行 ghcr.io/amancode22/deeperseeker@sha256:<上面输出的digest>
```

标签：`pyproject.toml` 里的完整版本（如 `6.0`）、主版本线（如 `6`）和 `latest`——三者都源自同一个字段，镜像标签永远不会和包版本对不上。同样的用法适用于 `podman pull` / `podman run`。

### 方式二——Docker Compose

源自 issue [#3](https://github.com/AmanCode22/deeperseeker/issues/3)；`podman-compose` 同样可用。

```bash
cp .env.example .env        # 填写 DEEPSEEKER_ADMIN_PASSWORD
docker compose up -d        # 或：podman-compose up -d
```

注意：`docker-compose.yml` 只绑定 `127.0.0.1:4000`（仅本机访问）——需要对外暴露请自行修改端口映射。

仪表盘：`http://localhost:4000/`

### 方式三——本地 Python + uv（开发推荐）

先安装 [uv](https://docs.astral.sh/uv/)：

```bash
# linux / macOS
curl -LsSf https://astral.sh/uv/install.sh | sh
```

```powershell
# windows
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
```

然后：

```bash
uv sync
source .venv/bin/activate   # 或 activate.fish / activate.zsh
cp .env.example .env
uv run deeperseeker
```

### 方式四——本地 Python（不用 uv）

```bash
python3 -m venv .venv
pip install -r requirements.txt
cp .env.example .env
python3 app.py
```

> **说明：** 感谢 [@alan7383](https://github.com/alan7383) 的 PR <https://github.com/AmanCode22/deeperseeker/pull/16>，Playwright 与 Chromium 已弃用并仅作后备保留（DeepSeek 不会对使用 Android 客户端头信息的请求执行 AWS WAF）。除非回退到后备的 cookie 采集机制，否则不再需要运行 `playwright install chromium` 或 `xvfb-run`——而且这样更省内存，也比 cookie 采集稳定得多。

## 认证令牌配置

1. 打开无痕窗口 → `chat.deepseek.com` → 登录。
2. 控制台（F12）执行：`JSON.parse(localStorage.getItem("userToken")).value`
3. 把返回的原始令牌字符串粘贴到仪表盘（`/dashboard`）。关闭无痕窗口。

## 配置（`.env`）

把 `.env.example` 复制为 `.env` 并填入你的私密配置。下表是全部可调项；默认值足够合理，通常只需要设置管理员口令一对。

| 变量 | 说明 | 默认值 |
|---|---|---|
| `DEEPSEEKER_API_KEY` | 访问端点所需的 Bearer API 密钥；若未设置，首次启动会自动生成强密钥、打印一次并持久化到 `api_key.txt` | 自动生成 |
| `DEEPSEEKER_ADMIN_USER` | 仪表盘登录用户名 | `admin` |
| `DEEPSEEKER_ADMIN_PASSWORD` | 仪表盘登录密码——默认凭据 + 非回环绑定会拒绝启动（见安全默认值） | `admin` |
| `DEEPSEEKER_ALLOW_DEFAULT_ADMIN` | 明确接受在非回环绑定上使用 admin/admin（不推荐）；只有字面量 `1` 能解锁启动 | 未设置 |
| `HOST` | 裸机运行时的绑定地址（`127.0.0.1` 仅本机，`0.0.0.0` 对外暴露） | `127.0.0.1` |
| `PORT` | 服务端口 | `4000` |
| `DEEPSEEKER_MAX_HISTORY_TOKENS` | 首条消息提示构建时注入历史的 token 上限 | `24000` |
| `DEEPSEEKER_MAX_TOOL_RESULT_TOKENS` | 注入工具结果的 token 上限 | `12000` |
| `DEEPSEEKER_MEMORY_LIMIT_TOKENS` | 触发滚动的已记忆上下文上限（观测值） | `393228` |
| `DEEPSEEKER_FIRST_MESSAGE_TOKENS` | 单条首条消息的可接受上限（观测值，仅供参考） | `974848` |
| `DEEPSEEKER_MAX_OUTPUT_TOKENS` | 单次响应输出上限（观测值，由 `/v1/models` 上报） | `8192` |
| `DEEPSEEKER_ROLLOVER_SAFETY_TOKENS` | 记忆上限之下预留的安全余量，提前触发摘要滚动 | `24000` |
| `DEEPSEEKER_MAX_SUMMARY_TOKENS` | 模型生成的交接摘要预算 | `4096` |
| `DEEPSEEKER_PER_TOOL_RESULT_TOKENS` | 单条工具输出的上限（防止一条巨型输出吃光预算） | `2000` |
| `DEEPSEEKER_RATE_LIMIT_COOLDOWN` | 被限流的 token 冷却多少秒后自动回到轮换池 | `60` |
| `DEEPSEEKER_TOKEN_CONCURRENCY` | 单 token 软并发上限；满载的 token 只在无其他空闲时使用 | `8` |
| `DEEPSEEKER_QUARANTINE_AFTER_403` | 连续多少次 403 会把 token 移出轮换，直到手动恢复（`0` 关闭升级） | `3` |
| `DEEPSEEKER_MAX_UPSTREAM_ATTEMPTS` | 一轮对话在池内的重试次数，用尽后才把上游错误返回给客户端 | `3` |
| `DEEPSEEKER_MAX_USER_MESSAGE_TOKENS` | 提示内最新一条用户消息的硬上限；超过后按尾部截断并加标记，而不是让上游失败 | `900000` |
| `DEEPSEEKER_PROMPT_OVERHEAD_TOKENS` | 滚动估算时加上的固定开销，覆盖工具模式、系统扩写与原始历史中看不到的摘要 | `4096` |
| `DEEPSEEKER_MODE` | `bridge`（逆向网页会话）或 `official`（官方 API 直通）。必须显式设置——没有任何自动回退 | `bridge` |
| `DEEPSEEKER_OFFICIAL_API_KEY` | `official` 模式使用的官方 API 密钥（来自 platform.deepseek.com）。官方模式下必填 | 未设置 |
| `DEEPSEEKER_OFFICIAL_BASE` | 官方模式下官方 API 基址覆盖 | `https://api.deepseek.com` |

## API 端点与用法

- **OpenAI Base**：`http://localhost:4000/v1`
  - `POST /v1/chat/completions`（流式与非流式）
  - `POST /v1/responses`（Responses API）
  - `GET /v1/models`
  - `POST /v1/files`
  - `GET /v1/files/{file_id}`
  - `GET /v1/files/{file_id}/content`
- **Anthropic Base**：`http://localhost:4000`
  - `POST /v1/messages`（也在 `/messages`）
  - `POST /v1/files/upload`
- **认证密钥**：在 `.env` 中配置（`DEEPSEEKER_API_KEY`）

### 模型

`v4.1flash`——唯一的默认 DeepSeek 模型（具备视觉能力：`vision` 不再需要单独的模型层）。无论客户端发送什么 `model` 值，都由它服务（`instant`、`expert`、`vision`、`anthropic/claude-*` 等旧别名都会被接受并归一化为 `v4.1flash`）。同时以 `anthropic/claude-v4.1flash` 的形式列出，供 Claude Desktop 自动发现。未发送 `model` 时，请求默认使用 `v4.1flash`。

## 官方 API 模式（退路）

设置 `DEEPSEEKER_MODE=official` 和 `DEEPSEEKER_OFFICIAL_API_KEY` 后，同样的端点（chat、messages、models）改为服务**官方 API**而不是网页会话。模式切换对客户端完全透明：同样的认证（你的 `DEEPSEEKER_API_KEY`）、同样的模型 id（`v4.1flash` 映射到 `deepseek-chat`）、同样的工具调用、同样的用量统计（surface 为 `official`）。官方密钥永远不会出现在日志行或错误体里。当桥接挂掉（PoW 变更、WAF 拦截、页面改版）时，这是"几分钟而不是几小时"的退路——改环境变量、重启、继续干活；想切回来也是同样的操作。

## 功能特性

- **多令牌池**：在单 token 软并发上限内选择在途请求最少的 token（空闲最久者优先），并发请求因此摊到多个账号上，而不是全砸进一个账号。
- **基于上下文的会话选择器**：对规范化后的消息历史（截至最后一条 assistant 轮次）、模型与 API 密钥作用域计算 SHA-256 签名，用来匹配并续用已有的网页聊天会话。会话创建有锁保护，避免重复建会话。
- **摘要滚动上下文策略**：当会话累计上下文接近观测到的记忆上限（约 393K 输入 token）时，桥接会向模型要一份精简的交接摘要（在临时会话里生成），开一个新网页会话，并用该摘要加最新用户消息做种子。最近相关的工具调用/结果会保留（受 `DEEPSEEKER_MAX_TOOL_RESULT_TOKENS` 约束，另有更紧的单条上限 `DEEPSEEKER_PER_TOOL_RESULT_TOKENS`）；附件在摘要中以文字描述，不做转发。第一条消息无论多大都绝不滚动（首条消息路径可接受约 1M token）。`/v1/models` 声明的限额（`context_window`、`max_output_tokens`）反映的是观测行为，不是有保证的上游限额。摘要提示词要求模型把对话内容当作数据，绝不当作指令。
- **全量历史注入**：当会话签名不在数据库中或账号发生故障转移时，把完整对话历史注入新会话。
- **限流恢复与隔离**：429——或低于升级阈值的 403——会让 token 冷却 `DEEPSEEKER_RATE_LIMIT_COOLDOWN` 秒并自动回到轮换；而 401，或连续 `DEEPSEEKER_QUARANTINE_AFTER_403` 次 403 且中间没有成功轮次，会把它标记为 `QUARANTINED` 并永久移出轮换——被拒绝的凭据不是限流，每分钟重新武装一次正是账号被封的方式。除了仪表盘上的 **Restore**（`POST /tokens/{id}/restore`），没有任何路径能离开隔离。上游 401/403/429 时，坏掉的网页会话被丢弃，该轮在*另一个*池内 token 上以抖动退避重试——最多 `DEEPSEEKER_MAX_UPSTREAM_ATTEMPTS` 次，然后返回规范的 API 错误——重试带全量历史注入，账号维度的文件会先重新挂到该 token 上；桥接只在你添加的 token 之间轮换，绝不会自己注册新号。池视图按别名显示每个 token 的状态、在途数、最近使用与剩余冷却时间，不含任何凭据信息；迟到的在途成功无法解除隔离。在途计数按进程统计，多 worker 部署只能看到自己 worker 的数字。
- **长上下文韧性**：上游网页会话失败或返回空响应（如上下文溢出）时，坏会话被丢弃，请求在全新会话上以压缩、限 token 的历史注入重试一次；不可恢复的上游错误以规范的 JSON API 错误返回，而不是裸 500。
- **工具调用与流式**：SSE 流式输出，think 标签跨 chunk 边界重组（不截断），多格式工具调用解析——DSML XML、XML 块、`<function_call>` 块与 JSON——统一映射为 OpenAI/Anthropic 工具模式。
- **文件与视觉**：base64/URL 图片提取（含 SSRF 防护）与文档上传流式处理。图片和文档通过 `ref_file_ids` 直接引用——视觉的文件分叉步骤（`fork_file_task`）已移除，因为 `v4.1flash` 原生支持视觉。
- **用量统计**：仪表盘展示最近 30 天按 天 × 脱敏密钥别名 的请求数、输入/输出 token 与估算成本——"哪个客户端烧了我的账号？"，诚实地回答。每个*完成*的轮次只记一行：失败尝试与流式重试绝不重复计数，token 按清洗后的回复计费（与 API usage 块上报的数字一致），成本标注为统一费率估算而非上游计量。读时聚合在一个月数据量下仍是毫秒级（索引化 `GROUP BY`，实测：约 3 万行 → 约 10 毫秒）。
- **Claude Desktop 兼容**：丰富的 `/v1/models` 能力元数据 + `anthropic/claude-v4.1flash` 别名，支持客户端自动发现。
- **加固的仪表盘**：会话 TTL、暴力破解锁定（5 次失败 → 锁 5 分钟）、CSRF 来源校验。

## 安全默认值

每次启动的行为，全部经过实际运行验证（P0-8 前后对照矩阵）：

- **未设置 `DEEPSEEKER_API_KEY`**（启动 1）：自动生成强密钥，启动时打印一次，并持久化到数据库旁的 `api_key.txt`，重启后保持不变。永远不做开放中继。
- **默认仪表盘凭据 + 回环绑定**（启动 2）：带响亮警告启动，提示设置 `DEEPSEEKER_ADMIN_USER` / `DEEPSEEKER_ADMIN_PASSWORD`。本地开发保持零配置。
- **默认仪表盘凭据 + 非回环绑定**（启动 3）：**拒绝启动**，并指明修复方法——设置凭据对、保持回环绑定，或设置 `DEEPSEEKER_ALLOW_DEFAULT_ADMIN=1` 显式接受风险（不推荐）。能碰到仪表盘的人就拥有整个令牌池；这正是搞死 ds2api 的"暴露式中继"配置，所以现在它死在启动时，而不是死在事后复盘里。

容器以非特权用户（uid 10001）运行；只有 `/app/data`（数据库、cookie 备份、生成的密钥）可写。**新建**的命名卷会自动继承该属主。由 P0-8 之前 root 时代镜像填充的旧卷里是 root 属主的文件——交接一次即可正常使用（反方向永远安全：交接过的卷在 root 时代镜像下同样能用）：

```bash
docker run --rm -u root -v deeperseeker_deeperseeker_data:/app/data alpine \
  chown -R 10001:10001 /app/data
```

## 故障排查

- **回复为空或空白，最后收到 HTTP 502** —— *症状：* 客户端收到 `Empty response from DeepSeek (no parseable SSE content)`（或上游错误事件），最终收到一个带 `error.request_id`（OpenAI 面）或消息尾部 `[trace: …]`（Anthropic 面）的 `502`。*服务现在的行为：* 该轮在池内重试——每次换一个 token，尝试间抖动退避——最多 `DEEPSEEKER_MAX_UPSTREAM_ATTEMPTS` 次；当所有尝试都返回空时，返回设计好的 502，相关 id 盖在响应体和 `X-Request-ID` 头里，上游来源的信息不会未脱敏地进入响应体。*要查什么：* 打开仪表盘池视图，找卡在 `RATE_LIMITED`/`QUARANTINED` 的 token；拿 `request_id`/`trace` 对照访问日志行，看该轮走过了哪些状态。
- **Agent 框架（Claude Code、Cline 等编程 harness）报 "Context limit" 错误** —— *症状：* harness 上报上下文超限失败，而普通聊天客户端一直正常。*根因：* 上游上下文属于**聊天会话**而不是 token——旧的朴素重试原样重发同一个超长上下文（harness 每轮都会重放全部历史），每次尝试都以同样方式失败。*服务现在的行为：*（1）上下文类失败（上游上下文措辞，或带上下文提示的空 SSE 错误）会让重试**强制走摘要滚动**——新会话用模型生成的交接摘要做种子，轮次带着完整连续性恢复，而不是死掉或截肢历史；（2）最新用户消息有硬上限（`DEEPSEEKER_MAX_USER_MESSAGE_TOKENS`，保留尾部），超过约 975K 上游上限的首条消息也能通过，而不是每次尝试都失败；（3）滚动估算把工具模式和提示开销计入（`DEEPSEEKER_PROMPT_OVERHEAD_TOKENS`），让主动滚动在记忆会话死掉**之前**触发，而不是之后。

## 价格（每 1M token，截至 2026-09-06）

唯一默认模型的统一高峰时段费率：

| 模型层 | 输入成本（缓存未命中） | 输出成本 |
|---|---|---|
| **DeepSeek V4.1 Flash**（`v4.1flash`） | $0.44 | $1.32 |

API 响应里的 `cost` 就用这套统一费率。

## 项目文档

- [Claude Desktop 配置指南](CLAUDE_DESKTOP_SETUP.md)——几分钟把 Claude Desktop 接上 DeeperSeeker。
- [docs/CLIENT_MATRIX.md](docs/CLIENT_MATRIX.md)——哪些客户端测过、何时、结果如何。
- [SECURITY.md](SECURITY.md)——安全问题如何上报。
- [CONTRIBUTING.md](CONTRIBUTING.md)——补丁的分支/测试/合并仪式。
- [CHANGELOG.md](CHANGELOG.md)——逐版本变更记录。

## 免责声明

仅供教育目的。本项目与 DeepSeek 无关联、未获其背书或赞助。请负责任地使用，并遵守 DeepSeek 的服务条款。
