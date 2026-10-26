# Web2API 对话反代 + 写作工作室

一个帮助你从零开始写小说的个人创作工具。

一个仓库两块能力：

| | 入口 | 做什么 |
|---|---|---|
| **写作工作室** | `/studio`（站点首页即跳这里） | 小说与单篇文档的写作台：大纲 → 逐节生成 → 拼装导出，带记忆层、图谱与一致性告警 |
| **对话反代** | `/v1`、`/chat`、`/live`、`/admin` | 把 **DeepSeek / 通义千问 / 豆包** 的网页对话端，反向代理成 **OpenAI 兼容 API** |

写作工作室的生成调用走的是同一个网关、同一批网页账号（见 `app/llm/gateway.py`），**不额外接官方 API**：
也就是说下面所有的限流、风控与封号约束它一样都躲不开，适合低频、串行、过夜跑批。

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
# 1) 虚拟环境 + 依赖（requirements-dev.txt 会连 pytest / ruff 一起装；只跑服务可以只装 requirements.txt）
python -m venv .venv ; .venv\Scripts\Activate.ps1
pip install -r requirements-dev.txt
playwright install chromium

# 2) 复制环境变量（可选，改端口/口令）
copy .env.example .env

# 3) 构建前端（后端托管 frontend/dist；没构建时只有 /ui 的旧静态页兜底）
cd frontend ; npm install ; npm run build ; cd ..

# 4) 启动服务
uvicorn app.main:app --host 127.0.0.1 --port 8000
```

开发时可以直接 `python dev.py all`（后端 :8000 + Vite :5173，热更新）；`build` / `backend` / `frontend` 三个子命令单独起。
改完代码一键自检：`powershell -ExecutionPolicy Bypass -File scripts\check.ps1`（ruff → pytest → 前端构建）。

然后打开 **内嵌浏览器页** <http://127.0.0.1:8000/live>：选站点 → **连接** → 在实时画面里直接登录（扫码/密码/滑块）→ **检测登录**。登录态持久化后长期复用，**全程无需命令行**。需要精修解析时，在同一页点**开始录制接口**、在画面里发一句“你好”、再**停止录制**即可写入 fixtures。

> `/live` 画面显示的**就是后台发消息用的那个标签页**（不再是单独的标签），所以 `/v1` 请求过程中打出的字、弹出的验证码都能在画面里直接看到并操作。切到聊天页再切回来会自动恢复上次的连接（不用再点“连接”）。
>
> 「断开」只停推屏、不关浏览器进程：后台聊天还要复用这个标签页，反复重启也慢。所以断开后管理页的「浏览器」仍然是亮的，这是正常的；要连进程一起关掉、让那个点变灰，点「关闭浏览器」（`POST /live/close/<key>?full=true`），下次聊天会重新拉起。

命令行兜底（不用 UI 时）：
```powershell
python -m scripts.login qwen      # 弹出浏览器登录
python -m scripts.recon qwen      # 抓取真实对话接口
```

其他界面：
- 对话：<http://127.0.0.1:8000/chat>
- 管理面板（登录态/模型/模式概览）：<http://127.0.0.1:8000/admin>

## 写作工作室（`/studio`）

入口 <http://127.0.0.1:8000/studio>（就是站点首页）。生成靠的是网页账号，所以先按上面「快速开始」在 `/live` 里登录至少一个站点。

两种形态共用一套节点树与编辑器，按作品的 `kind` 切换：

| 形态 | 层级 | 长期记忆 | 专属能力 |
|---|---|---|---|
| `novel` 小说 | 卷 / 章 | 设定卡 + 实体/关系/事件/伏笔图谱 | 节拍卡、章纲；硬规则：死人复活 / 归属冲突（物品双持）/ 时间倒退 / 已毁地点 / 伏笔超期 |
| `article` 文档 | 节（单篇）/ 讲 → 节（系列） | 素材 / 事实卡 / 术语 | 模板大纲；硬规则：事实过期 / 缺引用 / 术语不统一 / 结构不完整 |

一条完整主线：

1. 建作品（选 kind / subtype / 模板）→ 生成大纲 → 逐节生成（`POST /studio/works/{id}/pipeline` 走 SSE 看着它写，或 `POST …/batch` 落库入队关页面不管）
2. 每节生成前按桶装配上下文（S0–S8 + S_src；novel 默认 28,000 字符、article 18,000）。`POST /studio/context/preview` 只装配不花钱，能看到每桶占多少、丢了什么——**调 prompt 靠这个，不然就是盲调**
3. 生成后可选自动后处理：分块索引 → 三段式摘要（事件 / 变化 / 钩子）→ 图谱抽取 → 一致性检查 → 5 维打分
4. 全文拼装 `POST …/assemble`，导出 `GET …/export`（Markdown）
5. 选中任意段落可单独加工（重写 / 压缩 / 扩写 / 润色 / 翻译，`POST /studio/transform`），多候选横向比较

无人值守那层：单 worker 串行队列 + 优先级 + 幂等入队；重启把 `running` 退回 `queued` 自动续跑；
遇到风控或登录失效整队暂停；每 `triage_every` 节抽检一次，连续两次判「重写」就熔断停队。

检索与记忆：正文/素材切成 700 字块进 FTS5（jieba 预分词），`index_units` 记内容哈希做增量重建（没改过的节点直接跳过）。
**向量检索尚未接入**（`chunk_vectors` 表还没建，属可选的 M5）。

检查规则分两类：

- **硬规则 9 条**（小说 5 + 文档 4，即上表「专属能力」那串）：结构上必然错，报 `error/warn` 并参与「设定合规」扣分；
- **软规则 4 条**（`address_drift` 称呼漂移 / `setting_conflict` 设定冲突（目前只判年龄）/ `absolute_claim` 绝对化表述 / `number_unsourced` 数字无出处）：
  **零 LLM 调用**，一律 `info`、不扣分、不阻塞定稿，每节每规则封顶 3 条，措辞永远是「请确认」——改判交给人。
  哪几条开在每个文种的 `checks` 里自己选：小说默认开前两条，文档按子类只开绝对化表述 / 数字无出处。

告警在右栏分「硬 / 待确认」两组渲染，点「定位」会高亮正文对应片段；图谱除了列表还能开力导向视图
（虚线圈 = 已死/已毁，虚线边 = 已过期关系）。

内容类型全部配置化：`content-types.yaml`（`kinds → scales → subtypes` 三层合并；预算、启用的上下文桶、层级、`checks`/`checks_append`），**新增文种不改代码**；
改完调 `POST /studio/content-types/reload` 立即生效，前端下拉框的数据源是 `GET /studio/content-types`。

业务数据落 `storage/studio.sqlite`，与对话库 `sessions.sqlite` 分开。清书架/清孤儿行：`python -m scripts.cleanup_works`（默认试运行、删前自动备份）。

设计与实测都写在 **`docs/novel-studio-tech-design.md`**（基线数据见 `docs/m0-baseline.md`，文种约定见 `docs/content-types.md`）。
界面走深色「墨与金」：唯一强调色 `#D9A45B`、正文衬线 17px / 行高 1.9、编辑区宽度封顶 646px。
完整规范由 `ui-ux-pro-max` 生成在本地 `design-system/`（已 gitignore，不入库，随时可重新生成）。
其余章节都是反代部分。

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

## 模型与模式（快速 / 深度思考 / 联网 / 专家）

接口支持模型下拉、深度思考、联网、专家模式四类能力，但**各家页面实际具备哪些以 `config.yaml` 为准**：只声明页面真实存在的开关，页面没有的不要配（否则每次请求都会记 `missing` 告警）。千问新版页面目前只保留了快速模式；豆包新版页面只剩「快速 / 专家」两个档位（模型下拉），没有深度思考/联网开关；DeepSeek 只有「深度思考」与「智能搜索」两枚开关。

**一、模型名自带模式**（适合只会传 `model` 的客户端）

| 写法 | 实际请求 |
| --- | --- |
| `qwen-max` / `deepseek-chat` | 快速模式（思考、联网关） |
| `doubao-pro` / `doubao-lite` | 豆包「专家 / 快速」档位（页面无思考/联网开关） |
| `deepseek-chat-think` | 深度思考 |
| `deepseek-chat-search` | 联网搜索 |
| `deepseek-chat-expert` | 思考 + 联网 |
| `deepseek-reasoner` | 等价 `deepseek-chat-think`（模型自带思考） |
| `deepseek-reasoner-search` | 后缀可叠加 |

后缀是 `<基础模型>-quick/-think/-search/-expert`，`GET /v1/models` 只列出该 provider 在 `modes` 里声明了对应预设的别名；未声明的后缀仍会被识别，但静默降级为页面当前状态（不报错，也不切换）。

**二、请求参数**（`/v1/chat/completions` body 的额外字段）

| 字段 | 取值 | 含义 |
| --- | --- | --- |
| `mode` | `quick` `think` `search` `expert` | 直接选预设 |
| `thinking` | bool | 网页端「深度思考」 |
| `search` | bool | 「联网搜索」（DeepSeek 现叫「智能搜索」，文案已兼容） |
| `reasoning_effort` | `low` `medium` `high` | DeepSeek 的 `medium/high` 视为开思考 |
| `web_search_options` | 对象 | 只要出现就视为开联网（兼容 OpenAI 写法） |

优先级：`mode` 选预设，`thinking`/`search`/`reasoning_effort` 再覆盖具体开关；两者都高于模型名后缀与模型自带特性，未指定时按快速模式兜底。每轮会先把页面拉到确定状态（没指定的开关显式关掉），避免上一轮的「深度思考/联网」残留影响本轮——这只对 `ui` 段里配了定位文案的开关生效，没配的开关不会被触碰。

```python
client.chat.completions.create(
    model="deepseek-chat",
    messages=[{"role": "user", "content": "帮我查下今天的汇率"}],
    extra_body={"search": True},   # OpenAI SDK 的额外字段放这里
)
```

**三、能力发现**

`GET /v1/capabilities` 每个 provider 返回 `models[]`（id + 网页端文案）、`modes[]`、`features[]`、`aliases[]`，以及最近一次页面模式对齐结果 `last_mode_status`。前端聊天页的模型下拉按 provider 分组，「深度思考 / 联网 / 专家模式」三枚按钮按这份能力表启用。

**四、文案校准（站点改版时）**

1. 打开 `/live?provider=qwen`（或 doubao / deepseek）并点「连接」；
2. 点「探测页面模式」，面板会列出页面上的可点控件与 `开/关` 状态，悬停可看 `aria-pressed`/`class`；`config.yaml` 里已声明的模型名与开关文案排在最前（模型下拉常在页面顶部，不只探输入框一带）；
3. 绑定行默认只露必填的两样：**模型下拉的触发文案**与**各模型在页面上的显示文案**；「思考 / 联网 / 场景」收在「高级」里（有旧绑定时会自动展开）。绑定时不用在下拉里翻文案：**直接点探测出来的那个 chip**，弹层里选「绑给谁」（思考 / 联网 / 触发器 / 某个模型 / 新场景）即可。点「保存绑定」即写入 `storage/ui-bindings.yaml` 并立即热加载，**不用改 `config.yaml`、也不用重启**。模式预设会按绑定自动推导：始终有快速，绑了思考/联网就有对应模式，两个都绑才有专家模式，每个场景各出一个同名模式；
4. 点「应用模式」（即 `POST /live/mode/<key>`）当场验证，成功后管理页的「最近对齐」显示绿色；
5. 绑定不准就重新探测后再选；想回到 `config.yaml` 的出厂写法点「清除」（即 `DELETE /live/ui-bindings/<key>`）。手动改过 `config.yaml` 时仍可点「重载配置」让其生效。

绑定面板底下那行「体检」是本地提示，不写进任何文件：探不到任何带 `开/关` 状态的控件时，思考/联网 会被自动改回未绑定（这类页面本来就没有这两个开关，硬绑只会每轮报缺失）；把某个 chip 同时绑给两个开关、或绑成模型下拉的文案时，会直接红字提示。

后端还会兜一道同样的检查（`apply_mode` 的 `misbind`）：某个「开关」绑的文案撞上了模型文案、或与另一个开关绑了同一个控件时，**目标为关直接跳过不点，撞模型文案的连「开」也拒绝点**（那一下点开的是模型菜单，等于每轮请求都在动页面），日志与「最近对齐」里写作 `疑似绑错`。看到这个词就去 `/live`「高级」把它改回未绑定，别指望重试能过。

绑定的状态判定默认看 `aria-pressed`/`aria-checked`，读不出来时再按 `active_class` 正则判断；两者都没有的控件需要 `blind` 盲点，目前只在 `config.yaml` 的 `ui` 段里手写（绑定面板的「场景」候选只列带 `aria` 状态的控件），探测结果悬停可看该控件有没有 `aria` 状态可依据。

自定义场景键只接受 `^[a-z][a-z0-9_]{0,23}$`（小写字母开头），不能与内置的 `thinking/search/model/quick/think/expert` 重名，最多 8 个。绑定后即可用 `extra_body={"mode": "code"}` 或模型名后缀 `qwen-max-code` 调用；快速模式会自动把上一轮留下的场景标签关掉，不会串到下一轮。

模式对齐失败不会中断回答，只降级成告警并记 `missing`；设 `W2A_STRICT_MODE=1` 才会把未对齐当错误抛出。

> 千问的 HTTP 快路径（`W2A_QWEN_HTTP=1`）目前只确认了 `chat_mode=quick`（录制值）；`deep_search` 与非快速模式的取值是推断的，未实测前建议先不开启快路径，用浏览器路径对比一次。需深度思考/专家模式时，未校准的字段不会下发。

## 配置

- `config.yaml`：provider 开关、首页地址、模型列表、并发上限、全局每分钟限流（非敏感）。
- `.env`（由 `.env.example` 复制）：`W2A_HOST/PORT`、可选 `W2A_API_KEY`（同时保护 `/v1`、`/admin/*`（`/health` 除外）、`/live` 与 `/studio/*` 的 REST 与 WebSocket）、`W2A_STORAGE_DIR`。
- 前端填口令：设了 `W2A_API_KEY` 后，SPA 左下角的「钥匙」按钮即为入口，填入后先拿 `/v1/models` 校验再保存（存 `localStorage`），此后 `/v1`、`/admin`、`/live`、`/studio` 的 REST 统一带 `Authorization: Bearer`，WS 带 `?token=`。服务端没设口令时留空即可。`GET /health` 返回 `auth_required` 用来判断要不要填。
- 浏览器跨域/WS 来源：默认只放行本机与 `5173` 开发端口，需要时用 `W2A_ALLOW_ORIGINS` 追加（逗号分隔，`*` 全放开）。
- `content-types.yaml`（项目根）：写作工作室的文种与模板配置。启动时按它覆盖库内内置模板（`is_builtin=1`），用户自建模板不动；
  写坏了不会让服务起不来（解析失败回落内置兜底，接口返回里带 `error`）。改完调 `POST /studio/content-types/reload` 就地生效。
- 写作业务库：`<W2A_STORAGE_DIR>/studio.sqlite`（默认 `storage/studio.sqlite`），表结构见 `app/studio/schema.py`；
  `DELETE /studio/works/{id}` 会连作品自己的数据一起清，但 `jobs`/`runs`/`index_units`/`chunks_fts` 四张不受外键保护，
  由 `delete_work()` 手动清（历史残留用 `cleanup_works --sweep-orphans`）。
- 页面绑定覆盖层：`storage/ui-bindings.yaml`（已 gitignore），由 `/live` 的「绑定」行写入；加载时覆盖 `config.yaml` 的 `ui` 与 `models[].label`，并按绑定的开关重建模式（标准四模式 + 每个自定义场景各一个同名模式；`body_patch`/`label` 仍沿用 `config.yaml`）。删掉该文件即完全回到 `config.yaml`。
- 热加载：改完 `config.yaml` 后 `POST /admin/reload` 就地生效（模型/模式/并发/限流都会重建，无需重启；`GET` 会返回 405 提示），`/live` 的「页面模式」面板里也有同名按钮。保存页面绑定会自动触发同样的热加载。
- 千问 HTTP 快路径开关：环境变量 `W2A_QWEN_HTTP=1`（需已 recon 出 `fixtures/qwen.json`）。
- 真流式开关：`W2A_CDP_STREAM=1`（默认开）；异常时可设 `0` 退回整段读取。
- SSE 心跳：`W2A_HEARTBEAT_SEC=15`（秒），思考阶段长期无增量时发注释行 `: ping`，防反代超时。
- 流式空闲超时：`W2A_STREAM_IDLE_TIMEOUT=60`（秒）。
- 人机验证：`W2A_AUTO_SLIDE=1` 自动处理（滑块直接拖）；`W2A_HUMAN_WAIT_SEC=60` 遇到只能人工做的图片点选验证码时，在有头浏览器里等人做完的最长秒数（`0` 不等）；`W2A_RISK_COOLDOWN=60` 验证失败后的风控冷却秒数。
- 最小请求间隔：`config.yaml` 里每个 provider 的 `min_interval_sec`（秒，从上一次回复结束算起）。千问默认 3 秒，用于降低 RGV587 人机验证触发率；`0` 关闭。

## 目录结构

```
app/
  main.py runtime.py config.py
  api/      openai_routes.py  admin_routes.py  live_routes.py  conversation_routes.py  studio_routes.py  schemas.py
  core/     base.py modes.py registry.py sse.py store.py ratelimit.py errors.py blocked.py control_tokens.py
            auth.py alerts.py cut.py request_log.py ui_bindings.py
  browser/  playwright_pool.py session_manager.py stream_capture.py cdp_stream.py live_browser.py network_recorder.py ui_modes.py site_session.py
  providers/ _replay.py _browser_base.py  qwen.py doubao.py deepseek.py
  llm/      gateway.py        # 写作流程与 provider 解耦，复用上面的 registry 与限流
  studio/   schema.py store.py content_types.py assembler.py prompting.py tokenize.py chunker.py
            indexer.py retrieval.py summarizer.py graph.py consistency.py soft_rules.py
            scoring.py tasks.py jobs.py jsonutil.py
frontend/   Vue 3 + Vite（/studio /chat /admin /live，首页重定向到 /studio；构建产物 dist 由后端托管）
design-system/  本地生成，已 gitignore（写作台的界面规范：颜色 / 字体 / 布局 / 组件 / 交付前检查）
web/        旧版静态页 chat.html admin.html live.html（前端未构建时兜底）
docs/       novel-studio-tech-design.md  m0-baseline.md  content-types.md
content-types.yaml   文种配置（kinds → scales → subtypes + 模板）
dev.py      frontend / backend / build / all
scripts/    login.py  recon.py  check.ps1
            smoke_studio.py  smoke_m1_tail.py  smoke_m2.py  smoke_m3.py  smoke_m4.py  smoke_soft_rules.py
            measure_baseline.py  shot_studio.py  cleanup_works.py
tests/      pytest（全量 125 例；反代与写作两侧都有覆盖，写作相关的用 tmp_path 建临时库，不碰 storage/）
fixtures/   <由 recon 生成，已 gitignore>
storage/    <浏览器 profile / 票据 / sessions.sqlite / studio.sqlite / backups，已 gitignore>
```

`scripts/` 下的验收脚本分两类，账号被风控时别看错：

- **完全离线**（不起服务、不发起任何生成调用）：`smoke_soft_rules.py`（只读正文跑软规则）、`cleanup_works.py`（清书架 / 清孤儿行，默认试运行）；
- **要先起服务**（跑本机 HTTP 接口）：`smoke_studio.py`（默认只读，`--generate` 才真生成）、`smoke_m3.py`（默认不调模型，`--llm` 才真实抽取）、
  `smoke_m1_tail.py` 与 `smoke_m2.py`（默认会真跑一次，`--skip-llm` 只验不调模型的部分）、`smoke_m4.py`（走队列，会生成）、
  `measure_baseline.py`（M0 基线实测，`--dry-run` 只看计划）、`shot_studio.py`（Playwright 截图，不发消息）。

## 常见问题

### `/v1` 或 `/live` 里提问被固定文案拒绝

错误形如 `{"error": {"type": "content_filter", "code": "risk_control", ...}}`，`message` 里带上游原文。这不是模型回答，而是站点策略层的固定卡片（千问的 `mime_type` 通常是 `multi_load/iframe`），常见原因按概率排序：

1. 内嵌浏览器的持久化 profile 处于**未登录游客态**（在 `/live` 里退出账号后就是这个状态）；
2. 账号/设备指纹被风控：普通 Chrome 能正常回答、内嵌浏览器不能，基本就是这条；
3. 问题本身命中内容审核。

排查顺序：`GET /live/debug/<key>` 看登录判定与 cookie 名 → `GET /live/loggedin/<key>` 应为 `logged_in: true` → 在 `/live` 里问一句 `1+1` → 仍被拒就备份并删除 `storage/browser-profiles/<key>` 与 `storage/session/<key>.json`，把该 provider 的 `headless` 改成 `false` 后重新登录。

> `/live` 与 `/v1` 共用同一份 profile，而且共用**同一个标签页**：在 `/live` 里退出登录，`/v1` 也会同时变成游客态；反过来，`/v1` 触发的人机验证也会直接摆在 `/live` 画面里。

### `/live/loggedin/<key>` 显示 false，但页面里明明登录了

这是登录判定误报，不是真的掉登录。判定依赖的 cookie 名单是硬编码的，站点改版（例如千问从 `tongyi.aliyun.com` 迁到 `chat.qwen.ai`，又迁到 `qianwen.com`）后 cookie 全改名就会失效。现在会依次用「sentinel cookie → 特征 cookie 名 → localStorage → 页面外观（头像/登录按钮）」兜底判定，返回体里的 `cookie_names` 是实际存在的 cookie 名（不含值），可用于核对。

`GET /live/debug/<key>` 会返回页面地址、标题、登录判定、cookie 名与 localStorage 键名，排查时先打这个接口。

### 页面刚打开就发送会被判机器

抓包页如果每次请求都重新加载、并在 1 秒内按 Enter，前端安全 SDK（阿里 baxia 等）还没来得及计算请求签名，上游会直接回固定拒绝卡片——这和账号无关，真人手打有自然停顿所以正常。现在默认复用已打开的抓包页，只在冷启动时预热 2.5 秒（`W2A_WARMUP_MS`，设 0 关闭）。

### 豆包报“系统繁忙”或拿到一条空回答

豆包的风控不返 HTTP 错误码，而是在 `200` 的 SSE 里回一条 `event: STREAM_ERROR`，实测两种：

| error_code | error_msg | 含义 |
| --- | --- | --- |
| `710022002` | 当前服务访问频繁，请稍后重试 | 游客态（全新 profile）发不出对话，页面同时弹登录框 |
| `710022004` | `rate limited`（extra 里 `type=verify`） | 该 profile/设备被风控，要求人机验证 |

以前这些载荷解析不出文本就被当成“没有增量”，最后返回一条 `content: ""` 的 200 响应，调用方什么线索都拿不到。
现在 `app/core/blocked.py:find_upstream_error()` 会认出业务错误，报 `type=upstream_error`
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

### 回答里出现 `[[inline_action_reply_1]]`、`[[doc_common_card_1]]` 之类的方括号垃圾

这是站点给网页前端渲染「追问建议 / 内联按钮 / 文档下载卡片」用的占位标记，不是模型正文。后端 `app/core/control_tokens.py` 会在下发前剔除，处理范围包括：

- 标记被流式分块截断（`[[doc_common_` + `card_1]]`）；
- 全角括号、标记内空格与零宽字符（`［［doc_common_card_1］］`）；
- **思考内容**（`reasoning_content`）里夹带的标记，与正文各用一个过滤器，互不干扰；
- 删掉独占一行的标记后遗留的连续空行会压平。

剔除数量会记一条 `已剔除上游控制标记 N 处` 日志；前端 `MessageBubble.vue` 再做一道同口径的兜底，用于显示历史消息。若仍然看到方括号，先用 `curl -N` 看原始 SSE 里到底是哪种形态（是否有不可见字符），再对照这条日志确认后端有没有命中。

### 千问弹了滑块，但自动过滑块每次都不通过（手动拖也一样）

先看 `/live` 日志里那一行：`自动过滑块 第N轮: 起点=(…) 距离=…px`。**距离本身是对的**（千问这条轨道实测 319～321px 才能到底，早期把 `.nc_scale` 的 253px 当轨道反而过不了）。若每轮拖完弹窗变成「验证失败，点击框体重试(error:xxxx)」，配合抓包里出现的 `/slide` + `/report?type=verifyFail`，说明站点是直接**判失败**而不是「没拖到位」——这是账号/IP 级风控，此时：

- 已经不会再连拖 3 轮：连续两次采样都是失败文案就收手，免得给风控连续喂失败样本（日志会写「已触发风控，停止重试」）；
- 弹窗一打开就已经是失败态时，先点一次刷新换题（换出题后重新定位手柄与轨道再拖）；找不到刷新入口或换不出新题就直接报风控，不再做无谓的拖拽；
- 你在 `/live` 画面里按下鼠标的瞬间，后端会给该 provider 发一个**让位信号**，正在跑的自动拖动（不管是 /live 按钮触发的还是后台聊天触发的）会在一个采样周期内停下并抬起左键，日志写「已让位给手动操作」；人手接管时不会再跟机器抢同一个手柄。一个页面只有一只鼠标，所以后端还会等它**真的松开左键**（最多 0.8 秒）才把你的按下事件送进去 —— 否则机器的 `mouse up` 会落在你按下之后，把这次拖拽抬掉，表现为「手动拖也不跟手」；
- 正确处置是**停手**：等几分钟、或换网络/换出口 IP 后再在 `/live` 里手动过一次，然后点「已验证·清除风控冷却」。同 IP 反复重试只会让风控等级越来越高。

### /studio 批量生成整队暂停（或一直报「作品不存在」）

队列只在两种情况下主动停：**风控/登录失效** 与 **抽检连续两次判「重写」**。
前者处置与聊天侧一样：`GET /live/debug/<key>` 看登录判定 → 去 `/live` 手动过一次验证 →
点「已验证·清除风控冷却」→ `POST /studio/queue/resume`（页面底部队列条上也有按钮）。
后者是止损阀，先把那一节正文改顺再 `resume`；已标 `failed` 的任务用 `POST /studio/jobs/{id}/retry` 重排（会把 attempts 清零）。

如果日志里反复出现「作品不存在」，那是 `delete_work()` 修好之前删掉的作品留下的孤儿 job 行：
跑一次 `python -m scripts.cleanup_works --sweep-orphans --yes` 清掉（默认试运行，加 `--yes` 才动手，动手前先备份）。

## 已知限制

- `stream:false` 请求仍会缓冲全部增量后合成一条完整响应；`stream:true` 走 CDP 逐块下发。
- 用量 `usage` 为粗略字符估算（约 4 字符 = 1 token），非真实计数。
- `max_tokens` / `stop` 是“尽力而为”的本地软截断（站点侧仍在生成）；`n>1` 直接 400；`tools/tool_choice` 暂不执行，只在响应头回 `X-W2A-Warning: tools-not-supported`。
- 限流拒绝（429）会带 `Retry-After` 头；客户端按它退避即可。
- 客户端中途断开时，已生成的部分内容仍会落库并标注「客户端中断生成」；刷新页面不会丢问题。
- `temperature/top_p` 仍只是接受但不透传。
- 页面结构/接口会变：失效时先重跑 `recon`，必要时微调 provider 的选择器与 `parse_payload`
  （对照 `fixtures/<key>.json` 里的 `response_sample`）。
- 自定义场景目前只是「点一下页面上带 `aria-pressed` 的 chip」：千问的代码/翻译/AI 写作等场景互斥关系由站点决定，同一请求同时开多个场景时页面表现以站点为准；聊天页仍只有「深度思考 / 联网 / 专家」三枚按钮，自定义场景请在 `/live` 里绑定后通过 `mode` 参数或 `<model>-<场景>` 别名调用。
- 切换模型/模式可能让站点新开一个会话（豆包换模型最明显），上游上下文会重置，`conversation_id` 映射不会自动迁移。
- 模型是否已切换只看「按钮/下拉这类控件上的文字」，正文里提到某个模型名不算数（避免误判成已切换而一直用错模型）；代价是模型在下拉里显示的文案若与 `models[].label` 不一致，会多走一次「打开下拉再选一次」。
- 模式切换本身有 0.3～1.5 秒开销；连续用同一模式不会重复点击。未校准的模式在千问 HTTP 快路径会回落到浏览器路径，速度更慢。
- 千问网页端改版后（`qianwen.com`）只剩输入框下方的「快速」chip，没有深度思考/联网/模型下拉的稳定控件；`config.yaml` 里对应的 `modes`/`ui` 已先移除，调用 `qwen-*-think/search/expert` 会静默降级为页面当前状态。站点再次改版并出现新控件时，在 `/live` 里探测后直接用「绑定」行指回去即可，不必改 yaml。
- 豆包 2026-10 改版后输入区只剩「豆包 快速 / 豆包 专家」模型档位，原来的「深度思考 / 联网搜索」开关已不存在：`doubao-pro` 映射「豆包 专家」、`doubao-lite` 映射「豆包 快速」，联网由站点自行决定；「更多技能」里的视频生成/帮我写作/PPT 等是一次性技能入口，没有开/关状态，**不能**当自定义场景绑定（不是盲点又无法判定状态，除非站点将来补上 `aria-pressed`）。
- DeepSeek 页面只有「深度思考」与「智能搜索」两枚开关，没有场景 chip；这两个已按标准开关绑定，不需要再用「场景」行另加。
- 参考项目 `LLM-Red-Team/qwen-free-api` 曾被曝供应链投毒，本项目未使用其代码，仅参考协议思路。

写作工作室这边：

- **告警不阻塞定稿**：`PATCH /studio/nodes/{id}` 不查 `issues`，下拉框改成 `final` 就直接过（设计文档 9.5 已如实注明“没实现”）。
- **软规则只给 `info`**：不改正文也不扣分，到底算不算问题要人判；且只在作品 `checks` 里显式开着时才跑。
- 删节点（`delete_node()`）与删素材（`delete_source()`）现在会把 `chunks_fts` / `index_units`（以及节点相关的 `summaries`）一起清掉；
  在这之前删下的历史残留用 `python -m scripts.cleanup_works --sweep-orphans` 清（旧版只按 work_id 判，认不出「作品还活着、节点已没」的那些行）。
- **向量检索未接入**：换个说法的语义查询仍只能靠 jieba + FTS5 的关键词召回。
- 自动打分为 5 维，**不做多候选盲评**；`/transform` 的候选只并排列出来给人比。
- 模板里的 `target_words` 是软约束，实测偏差约 ±20%~40%。
- 写作与聊天共用同一个网页账号与**同一个标签页**：`/studio` 正在批量生成时去 `/live` 退出登录，写作会跟着掉进游客态。

## 免责声明

本项目为技术学习与个人效率工具，使用者需自行承担违反平台条款及账号受限的风险。请勿用于商业用途或对外提供服务。
