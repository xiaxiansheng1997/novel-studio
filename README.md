# Web2API 对话反代

把 **DeepSeek / 通义千问 / 豆包** 的网页对话端，反向代理成 **OpenAI 兼容 API**，并自带极简聊天界面。
用你自己的网页账号，接入 ChatBox / LobeChat / IDE 插件 / openai SDK 等任意 OpenAI 兼容客户端。

> ⚠️ 合规与安全：本项目通过逆向网页端实现，可能违反各家服务条款。**仅限个人学习、本机自用**；
> 不要对公网暴露、不要分发、不要高并发刷量。DeepSeek 与豆包**有明确封号风险**，请低频使用。

## 它是怎么工作的

三家网页端都有各自的风控，纯 HTTP 逆向不可持续，因此采用 **Playwright 真实浏览器** 路线：

| 站点 | 登录态 | 反爬门槛 | 本项目落地方式 |
|---|---|---|---|
| 通义千问 | `login_tongyi_ticket`（约 1 个月） | 低 | 默认浏览器捕获；可选 HTTP 快路径（`W2A_QWEN_HTTP=1`） |
| 豆包 | Cookie（签名 5–10 分钟失效） | 前端 `X-Signature` | 浏览器捕获：让前端自算签名，我们旁路 tee 响应流 |
| DeepSeek | token 约 24h | PoW + AWS WAF，封号严 | 浏览器捕获：让前端自算 PoW，我们旁路 tee 响应流 |

**关键设计**：不重新实现 PoW / 签名 / TLS 指纹。在真实登录的页面里驱动发送，
用 CDP `Network.streamResourceContent` 旁路读取对话接口的 SSE 响应（页面自身仍正常消费），
逐块转成 OpenAI chunk；CDP 不可用时自动回落为整段读取。因此**默认只需登录，无需 recon**；
recon（页面里的“录制接口”开关或 `scripts.recon`）仅用于启用通义 HTTP 快路径或精修解析器。

关于"不想每天登录"的现实：千问 ticket 约 1 个月，最能长期免登录；DeepSeek/豆包做不到彻底免登录，
只能靠**持久化浏览器 profile** 把重新登录频率降到最低，失效时管理面板会提示你重新 `login`。

## 快速开始

```powershell
# 1) 虚拟环境 + 依赖
python -m venv .venv ; .venv\Scripts\Activate.ps1
pip install -r requirements.txt
playwright install chromium

# 2) 复制环境变量（可选，改端口/口令）
copy .env.example .env

# 3) 启动服务
uvicorn app.main:app --host 127.0.0.1 --port 8000
```

然后打开 **内嵌浏览器页** <http://127.0.0.1:8000/ui/live.html>：选站点 → **连接** → 在实时画面里直接登录（扫码/密码/滑块）→ **检测登录**。登录态持久化后长期复用，**全程无需命令行**。需要精修解析时，在同一页点**开始录制接口**、在画面里发一句“你好”、再**停止录制**即可写入 fixtures。

命令行兜底（不用 UI 时）：
```powershell
python -m scripts.login qwen      # 弹出浏览器登录
python -m scripts.recon qwen      # 抓取真实对话接口
```

其他界面：
- 对话：<http://127.0.0.1:8000/ui/chat.html>
- 管理面板（登录态/模型概览）：<http://127.0.0.1:8000/ui/admin.html>

## 接入 OpenAI 兼容客户端

```python
from openai import OpenAI
client = OpenAI(base_url="http://127.0.0.1:8000/v1", api_key="unused")
resp = client.chat.completions.create(
    model="deepseek-chat",   # 前缀决定路由到哪个 provider
    messages=[{"role": "user", "content": "你好"}],
    stream=True,
)
```

`model` 前缀路由：`qwen-*` / `doubao-*` / `deepseek-*`。`GET /v1/models` 返回三家合并的模型列表。

## 配置

- `config.yaml`：provider 开关、首页地址、模型列表、并发上限、全局每分钟限流（非敏感）。
- `.env`（由 `.env.example` 复制）：`W2A_HOST/PORT`、可选 `W2A_API_KEY`、`W2A_STORAGE_DIR`。
- 千问 HTTP 快路径开关：环境变量 `W2A_QWEN_HTTP=1`（需已 recon 出 `fixtures/qwen.json`）。
- 真流式开关：`W2A_CDP_STREAM=1`（默认开）；异常时可设 `0` 退回整段读取。
- 流式空闲超时：`W2A_STREAM_IDLE_TIMEOUT=60`（秒）。
- 最小请求间隔：`config.yaml` 里每个 provider 的 `min_interval_sec`（秒，从上一次回复结束算起）。千问默认 3 秒，用于降低 RGV587 人机验证触发率；`0` 关闭。

## 目录结构

```
app/
  main.py runtime.py config.py
  api/      openai_routes.py  admin_routes.py  live_routes.py  schemas.py
  core/     base.py registry.py sse.py store.py ratelimit.py errors.py blocked.py control_tokens.py
  browser/  playwright_pool.py session_manager.py stream_capture.py cdp_stream.py live_browser.py network_recorder.py
  providers/ _replay.py _browser_base.py  qwen.py doubao.py deepseek.py
scripts/    login.py  recon.py
web/        chat.html  admin.html  live.html
fixtures/   <由 recon 生成，已 gitignore>
storage/    <浏览器 profile / 票据 / 会话库，已 gitignore>
```

## 常见问题

### `/v1` 或 `/live` 里提问被固定文案拒绝

错误形如 `{"error": {"type": "content_filter", "code": "risk_control", ...}}`，`message` 里带上游原文。这不是模型回答，而是站点策略层的固定卡片（千问的 `mime_type` 通常是 `multi_load/iframe`），常见原因按概率排序：

1. 内嵌浏览器的持久化 profile 处于**未登录游客态**（在 `/live` 里退出账号后就是这个状态）；
2. 账号/设备指纹被风控：普通 Chrome 能正常回答、内嵌浏览器不能，基本就是这条；
3. 问题本身命中内容审核。

排查顺序：`GET /live/debug/<key>` 看登录判定与 cookie 名 → `GET /live/loggedin/<key>` 应为 `logged_in: true` → 在 `/live` 里问一句 `1+1` → 仍被拒就备份并删除 `storage/browser-profiles/<key>` 与 `storage/session/<key>.json`，把该 provider 的 `headless` 改成 `false` 后重新登录。

> `/live` 与 `/v1` 共用同一份 profile：在 `/live` 里退出登录，`/v1` 也会同时变成游客态。

### `/live/loggedin/<key>` 显示 false，但页面里明明登录了

这是登录判定误报，不是真的掉登录。判定依赖的 cookie 名单是硬编码的，站点改版（例如千问从 `tongyi.aliyun.com` 迁到 `chat.qwen.ai`）后 cookie 全改名就会失效。现在会依次用「sentinel cookie → 特征 cookie 名 → localStorage → 页面外观（头像/登录按钮）」兜底判定，返回体里的 `cookie_names` 是实际存在的 cookie 名（不含值），可用于核对。

`GET /live/debug/<key>` 会返回页面地址、标题、登录判定、cookie 名与 localStorage 键名，排查时先打这个接口。

### 页面刚打开就发送会被判机器

抓包页如果每次请求都重新加载、并在 1 秒内按 Enter，前端安全 SDK（阿里 baxia 等）还没来得及计算请求签名，上游会直接回固定拒绝卡片——这和账号无关，真人手打有自然停顿所以正常。现在默认复用已打开的抓包页，只在冷启动时预热 2.5 秒（`W2A_WARMUP_MS`，设 0 关闭）。

### 回答末尾出现 `[[inline_action_reply_1]]` 之类的方括号垃圾

这是站点给网页前端渲染「追问建议 / 内联按钮」用的占位标记，不是模型正文。后端 `app/core/control_tokens.py` 会在下发前剔除（能处理标记被流式分块截断的情况），剔除数量会记一条 `已剔除上游控制标记 N 处` 日志；前端 `MessageBubble.vue` 再做一道兜底，用于显示历史消息。

## 已知限制

- `stream:false` 请求仍会缓冲全部增量后合成一条完整响应；`stream:true` 走 CDP 逐块下发。
- 用量 `usage` 为粗略字符估算（约 4 字符 = 1 token），非真实计数。
- 各家 `temperature/top_p/max_tokens` 等多被接受但忽略。
- 页面结构/接口会变：失效时先重跑 `recon`，必要时微调 provider 的选择器与 `parse_payload`
  （对照 `fixtures/<key>.json` 里的 `response_sample`）。
- 参考项目 `LLM-Red-Team/qwen-free-api` 曾被曝供应链投毒，本项目未使用其代码，仅参考协议思路。

## 免责声明

本项目为技术学习与个人效率工具，使用者需自行承担违反平台条款及账号受限的风险。请勿用于商业用途或对外提供服务。
