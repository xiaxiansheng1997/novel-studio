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
用注入脚本 `tee` 复制走对话接口的 SSE 响应，实时转成 OpenAI chunk。因此**默认只需登录，无需 recon**；
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

## 目录结构

```
app/
  main.py runtime.py config.py
  api/      openai_routes.py  admin_routes.py  live_routes.py  schemas.py
  core/     base.py registry.py sse.py store.py ratelimit.py errors.py
  browser/  playwright_pool.py session_manager.py stream_capture.py live_browser.py network_recorder.py
  providers/ _replay.py _browser_base.py  qwen.py doubao.py deepseek.py
scripts/    login.py  recon.py
web/        chat.html  admin.html  live.html
fixtures/   <由 recon 生成，已 gitignore>
storage/    <浏览器 profile / 票据 / 会话库，已 gitignore>
```

## 已知限制

- 非流式：三家都只有 SSE，服务端会缓冲全部增量后合成一条完整响应返回。
- 用量 `usage` 为粗略字符估算（约 4 字符 = 1 token），非真实计数。
- 各家 `temperature/top_p/max_tokens` 等多被接受但忽略。
- 页面结构/接口会变：失效时先重跑 `recon`，必要时微调 provider 的选择器与 `parse_payload`
  （对照 `fixtures/<key>.json` 里的 `response_sample`）。
- 参考项目 `LLM-Red-Team/qwen-free-api` 曾被曝供应链投毒，本项目未使用其代码，仅参考协议思路。

## 免责声明

本项目为技术学习与个人效率工具，使用者需自行承担违反平台条款及账号受限的风险。请勿用于商业用途或对外提供服务。
