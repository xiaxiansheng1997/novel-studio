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

> `/live` 画面显示的**就是后台发消息用的那个标签页**（不再是单独的标签），所以 `/v1` 请求过程中打出的字、弹出的验证码都能在画面里直接看到并操作。切到聊天页再切回来会自动恢复上次的连接（不用再点“连接”）。
>
> 「断开」只停推屏、不关浏览器进程：后台聊天还要复用这个标签页，反复重启也慢。所以断开后管理页的「浏览器」仍然是亮的，这是正常的；要连进程一起关掉、让那个点变灰，点「关闭浏览器」（`POST /live/close/<key>?full=true`），下次聊天会重新拉起。

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
- 人机验证：`W2A_AUTO_SLIDE=1` 自动处理（滑块直接拖）；`W2A_HUMAN_WAIT_SEC=60` 遇到只能人工做的图片点选验证码时，在有头浏览器里等人做完的最长秒数（`0` 不等）；`W2A_RISK_COOLDOWN=60` 验证失败后的风控冷却秒数。
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

> `/live` 与 `/v1` 共用同一份 profile，而且共用**同一个标签页**：在 `/live` 里退出登录，`/v1` 也会同时变成游客态；反过来，`/v1` 触发的人机验证也会直接摆在 `/live` 画面里。

### `/live/loggedin/<key>` 显示 false，但页面里明明登录了

这是登录判定误报，不是真的掉登录。判定依赖的 cookie 名单是硬编码的，站点改版（例如千问从 `tongyi.aliyun.com` 迁到 `chat.qwen.ai`）后 cookie 全改名就会失效。现在会依次用「sentinel cookie → 特征 cookie 名 → localStorage → 页面外观（头像/登录按钮）」兜底判定，返回体里的 `cookie_names` 是实际存在的 cookie 名（不含值），可用于核对。

`GET /live/debug/<key>` 会返回页面地址、标题、登录判定、cookie 名与 localStorage 键名，排查时先打这个接口。

### 页面刚打开就发送会被判机器

抓包页如果每次请求都重新加载、并在 1 秒内按 Enter，前端安全 SDK（阿里 baxia 等）还没来得及计算请求签名，上游会直接回固定拒绝卡片——这和账号无关，真人手打有自然停顿所以正常。现在默认复用已打开的抓包页，只在冷启动时预热 2.5 秒（`W2A_WARMUP_MS`，设 0 关闭）。

### 豆包报“系统繁忙”或拿到一条空回答

豆包的风控不返 HTTP 错误码，而是在 `200` 的 SSE 里回一条 `event: STREAM_ERROR`，实测两种：

| error_code | error_msg | 含义 |
| --- | --- | --- |
| `710022002` | 当前服务访问频繁，请稍后重试 | 游客态（全新 profile）发不出对话，页面同时弹登录框 |
| `710022004` | `rate limited`（extra 里 `type=verify`） | 该 profile/设备被风控，要求人机验证 |

以前这些载荷解析不出文本就被当成“没有增量”，最后返回一条 `content: ""` 的 200 响应，调用方什
么线索都拿不到。现在 `app/core/blocked.py:find_upstream_error()` 会认出业务错误，报 `type=upstream_error`
并把 `error_code` 和上游原文带在 `message` 里。另外：

- 豆包网页端**不登录不能聊**（已用全新 profile 验证：输入后立刻回 `710022002`，页面随后被踢回 `https://www.doubao.com/chat/?from_logout=1`），请先在 `/live` 里登录；
- `710022004` 且 `extra` 里带 `type=verify` 时，后端会先认题型再处理（`W2A_AUTO_SLIDE=0` 关掉整套自动处理）：
  - **滑块类**（阿里 baxia 等）：自动拖过去并重发一次；
  - **图片点选/拖拽类**（豆包实测就是这种：“体育运动常用的物品 / 请选择所有符合上述描述的图片，并拖拽到下方”）：要看着图做语义判断，机器代做不可靠，所以不再盲目拖。豆包默认 `headless: false`，后端会在那个 Chrome 窗口（也就是 `/live` 画面里的同一个标签页）里**等你手动勾选图片**，最多 `W2A_HUMAN_WAIT_SEC` 秒（默认 60，设 0 关闭），检测到验证码消失就自动把同一句话重发一次，回答照常返回；
  - 等不到人做完、或过完仍然被拒，才返回 `type=human_verify`（HTTP 503）并进入风控冷却。无头模式没人能操作页面，会直接给指引：去 `/live` 里做，过完点「已验证·清除风控冷却」。
- 还有一种「发不出去」和人机验证无关：豆包会随机弹「下载豆包电脑版 解锁本地任务能力」这类营销弹窗，它的遮罩盖住整个页面，点击输入框会一直等到超时（日志里是 `intercepts pointer events`）。现在发送前会自动关掉这类弹窗（带 captcha/验证/滑块 特征的弹窗不会被误关），实在关不掉就退化成 JS 聚焦 + 键盘输入继续发送；
- 流式回答讲到一半才冒出验证信号时，正文已经下发给用户，不再截断改判错误，只记一条冷却；
- 持续不行就备份并删除 `storage/browser-profiles/doubao` 后重新登录；
- 上游自己抽风（详情里带 5xx/timeout）会回 `503` 并提示“隔一会儿重试”，不会误引导你去查登录态。

### DeepSeek 只回了一个会话标题，或回答只到一半

DeepSeek 网页端 `/api/v0/chat/completion` 的 SSE 既不是 OpenAI 格式，也不是单一形态，而是三种混着发：

```
data: {"v":{"response":{"fragments":[{"type":"RESPONSE","content":"光合"}]}}}   # 快照
data: {"p":"response/fragments/-1/content","o":"APPEND","v":"作用是"}            # 打补丁
data: {"v":"植物"}                                                              # 逐字增量（主流）
```

另外标题事件的 data 是 `{"content":"会话标题"}`。旧版只认 OpenAI 风格 + 泛用取文（找 `content/text/delta` 键），结果：标题被当成回答、`{"v":"…"}` 的逐字增量全部丢掉（表现为只回几个字）。现在 `app/providers/deepseek.py` 分别处理快照 / 补丁 / 逐字增量，并显式丢弃标题与状态事件；`fixtures/deepseek.json` 里的 `response_sample` 可直接回放验证。

### 手动关掉浏览器窗口后，所有请求都报 `Target page, context or browser has been closed`

以前会一直坏到重启后端：`BrowserPool` 缓存了那个已死的 context，每次都拿去 `new_page()`。现
在会监听 context 的 `close` 事件即时除名（`is_open` 与 `/live/loggedin` 也会跟着变 `false`），`get_page()`
里再兜底一层：`new_page()` 失败就重启 context 重试一次。自己把窗口关了也不用再重启服务。

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
