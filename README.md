# 写作工作室（Novel Studio）

一个帮助你从零开始写小说的个人创作工具。

大纲 → 逐节生成 → 摘要 / 图谱 / 一致性检查 → 全文拼装导出，都在 `/studio` 一个页面里完成；
除小说外还能写单篇文档（`article`），两种形态共用同一套节点树与编辑器。

生成**不接官方 API**：底层用 Playwright 驱动 DeepSeek / 通义千问 / 豆包 的网页登录账号产出文本。
所以那三家的限流、人机验证与封号约束一样都躲不开——这套工具是按**低频、串行、过夜跑批**设计的，不是按并发量设计的。
驱动网页端的那层原先是一个独立的「对话反代」项目，现在作为底座留在仓库里，
细节（`/v1` OpenAI 兼容接口、模式与文案校准、风控排查细则）都在 **[`docs/reverse-proxy.md`](docs/reverse-proxy.md)**。

> ⚠️ 合规与安全：本项目通过逆向网页端实现，可能违反各家服务条款。**仅限个人学习、本机自用**；
> 不要对公网暴露、不要分发、不要高并发刷量。DeepSeek 与豆包**有明确封号风险**，请低频使用。

## 快速开始

```powershell
# 1) 虚拟环境 + 依赖（requirements-dev.txt 会连 pytest / ruff 一起装；只跑服务可以只装 requirements.txt）
python -m venv .venv ; .venv\Scripts\Activate.ps1
pip install -r requirements-dev.txt
playwright install chromium

# 2) 复制环境变量（可选，改端口/口令）
copy .env.example .env

# 3) 构建前端（后端托管 frontend/dist；没构建时只有 /ui 的旧静态页兜底，没有 /studio）
cd frontend ; npm install ; npm run build ; cd ..

# 4) 启动服务
uvicorn app.main:app --host 127.0.0.1 --port 8000
```

开发时可以直接 `python dev.py all`（后端 :8000 + Vite :5173，热更新）；`build` / `backend` / `frontend` 三个子命令单独起。
改完代码一键自检：`powershell -ExecutionPolicy Bypass -File scripts\check.ps1`（ruff → pytest → 前端构建）。

**开写之前先把账号登录上**：打开 <http://127.0.0.1:8000/live> → 选站点 → **连接** → 在实时画面里直接登录
（扫码/密码/滑块）→ **检测登录**。登录态落在持久化 profile 里长期复用，失效时管理面板会提示。
然后去 <http://127.0.0.1:8000/studio>（站点首页也跳这里）建作品。

## 一条完整主线

1. 建作品（选 kind / subtype / 模板）→ 生成大纲 → 逐节生成（`POST /studio/works/{id}/pipeline` 走 SSE 看着它写，或 `POST …/batch` 落库入队关页面不管）
2. 每节生成前按桶装配上下文（S0–S8 + S_src；novel 默认 28,000 字符、article 18,000）。`POST /studio/context/preview` 只装配不花钱，能看到每桶占多少、丢了什么——**调 prompt 靠这个，不然就是盲调**
3. 生成后可选自动后处理：分块索引 → 三段式摘要（事件 / 变化 / 钩子）→ 图谱抽取 → 一致性检查 → 5 维打分
4. 全文拼装 `POST …/assemble`，导出 `GET …/export`（Markdown）
5. 选中任意段落可单独加工（重写 / 压缩 / 扩写 / 润色 / 翻译 / 拆分 / 合并，`POST /studio/transform`），多候选横向比较

## 两种形态

| 形态 | 层级 | 长期记忆 | 专属能力 |
|---|---|---|---|
| `novel` 小说 | 卷 / 章 | 设定卡 + 实体/关系/事件/伏笔图谱 | 节拍卡、章纲；硬规则：死人复活 / 归属冲突（物品双持）/ 时间倒退 / 已毁地点 / 伏笔超期 |
| `article` 文档 | 节（单篇）/ 讲 → 节（系列） | 素材 / 事实卡 / 术语 | 模板大纲；硬规则：事实过期 / 缺引用 / 术语不统一 / 结构不完整 |

## 无人值守

单 worker 串行队列 + 优先级 + 幂等入队（`idem_key`）；重启把 `running` 退回 `queued` 自动续跑；
遇到风控或登录失效**整队暂停**；每 `triage_every` 节抽检一次，连续两次判「重写」就熔断停队。

页底队列条显示「排队 N · 完成 N · 约 X 分钟」与当前在跑的那一节，也有暂停/恢复按钮（`POST /studio/queue/resume`）；
已标 `failed` 的任务用 `POST /studio/jobs/{id}/retry` 重排（会把 attempts 清零）。
每一节用了哪个 model / provider、输入输出各多少字符、耗时与打分，都记在 `runs` 表里，成本可以回看
（用量是字符估算，不是真实 token）。

## 检索与记忆

正文与素材切成 700 字块进 FTS5（jieba 预分词），`index_units` 记内容哈希做增量重建（没改过的节点直接跳过）。
摘要分三段（事件 / 变化 / 钩子），装配上下文时按桶取用。
**向量检索尚未接入**（`chunk_vectors` 表还没建，属可选的 M5），换个说法的语义查询仍只能靠关键词召回。

## 改完稿子会发生什么

改正文不是只改那一行字，库里有一圈东西是从正文派生出来的。这一节说清楚改完之后各自往哪走：

- **摘要按内容指纹过期**。`summaries` 每行记着它是从哪一版正文算出来的（`ref_hash`），正文一改指纹就对不上；
  卷摘要与全书摘要是多节拼出来的，没有单一源文本，靠失效传播把它们的 `ref_hash` 置空。
  过期的摘要**不会进生成上下文**（S3/S4 只取新鲜的），被丢了哪几节写在「上下文」预览的 warnings 里，
  右栏「记忆」也会显示「N 节摘要待重算」——按钮文案跟着变成「重算过期摘要」，点下去只会补过期那些，不重烧没改过的。
- **每一版都留得回来**。每次「采用」与生成落库都会存一个版本，来源标成 AI 草稿 / AI 润色 / 手改，
  右栏「历史」页能列出、能跟当前正文并排比对、能点「用这一版」回滚。
  回滚前服务端会先把当下这份还没进版本表的正文存一版，所以点回滚不会吃掉你刚敲的几百字。
- **上下移会把章号一起带走**。树上的 ↑↓ 只跟同一个父节点下的紧邻兄弟互换；「第几章」这个数在库里存了十份
  （章节序、摘要序号、分块序、实体的登场/死亡章号、关系与事件与伏笔的章号、角色状态章号），
  交换时一起对调，否则「已死的角色在第 5 章出场」这类判定会跟着错位。
  内容没变，所以不重算索引与摘要，只把库标脏。
- **删除前自动留备份**。删一节（含整棵子树与全部历史版本）先写一份 JSON 到 `storage/backups/nodes/`，
  删作品先拷一份整库副本到 `storage/backups/works/`；**备份写不成就拒绝删除**，界面上会给出「备份失败，已拒绝删除」。
  找回一节：`python -m scripts.restore_backup --list` 看有哪些，`--name <文件>` 试运行，加 `--yes` 真恢复
  （原父节点还在就挂回去，没了就挂作品根；章号追加到末尾，再用 ↑↓ 挪回原位）。
  整库副本只列不给脚本改：那种找回是停服务后把文件盖回 `storage/studio.sqlite`。
  节点备份留最近 20 份、整库副本留最近 5 份，删的时候顺手清理。

## 检查规则

- **硬规则 9 条**（小说 5 + 文档 4，即上表「专属能力」那串）：结构上必然错，报 `error/warn` 并参与「设定合规」扣分；
- **软规则 4 条**（`address_drift` 称呼漂移 / `setting_conflict` 设定冲突（目前只判年龄）/ `absolute_claim` 绝对化表述 / `number_unsourced` 数字无出处）：
  **零 LLM 调用**，一律 `info`、不扣分、不阻塞定稿，每节每规则封顶 3 条，措辞永远是「请确认」——改判交给人。
  哪几条开在每个文种的 `checks` 里自己选：小说默认开前两条，文档按子类只开绝对化表述 / 数字无出处。

告警在右栏分「硬 / 待确认」两组渲染，点「定位」会高亮正文对应片段。

## 图谱

实体 / 关系 / 事件 / 伏笔从正文里抽出来存库，除了列表还能开力导向视图（虚线圈 = 已死/已毁，虚线边 = 已过期关系）。
抽取要真调模型（`smoke_m3.py --llm` 是它的验收入口），右栏「图谱」那页会给出实体 / 关系 / 伏笔的计数。

## 文种全部配置化

`content-types.yaml`（`kinds → scales → subtypes` 三层合并；预算、启用的上下文桶、层级、`checks`/`checks_append`），
**新增文种不改代码**；改完调 `POST /studio/content-types/reload` 立即生效，前端下拉框的数据源是 `GET /studio/content-types`。
启动时按它覆盖库内内置模板（`is_builtin=1`），用户自建模板不动；写坏了不会让服务起不来（解析失败回落内置兜底，接口返回里带 `error`）。
约定见 `docs/content-types.md`。

## 数据与清理

业务数据落 `storage/studio.sqlite`，与底座对话用的 `sessions.sqlite` 分开；表结构见 `app/studio/schema.py`。

`DELETE /studio/works/{id}` 会连作品自己的数据一起清，但 `jobs`/`runs`/`index_units`/`chunks_fts` 四张表不受外键保护
（FTS5 是虚拟表，接不到级联），由删除逻辑手动清；删单节、删素材同样会带走 `chunks_fts` / `index_units`（节点还多清 `summaries`，
否则已删章节会以摘要形式继续进 prompt）。历史残留用：

```powershell
python -m scripts.cleanup_works --sweep-orphans    # 默认试运行，加 --yes 才动手，动手前自动备份
```

清书架也走这个脚本（同样默认试运行）。

删除本身也留退路：删节/删卷写 `storage/backups/nodes/*.json`（正文 + 全部历史版本），删作品写
`storage/backups/works/*.sqlite` 整库副本；备份写不成就拒绝删除。找回见上面「改完稿子会发生什么」与
`python -m scripts.restore_backup --list`。

## 界面规范

深色「墨与金」：唯一强调色 `#D9A45B`、正文衬线 17px / 行高 1.9、编辑区宽度封顶 646px。
完整规范由 `ui-ux-pro-max` 生成在本地 `design-system/`（已 gitignore，不入库，随时可重新生成）。

## 配置

- `.env`（由 `.env.example` 复制）：`W2A_HOST/PORT`、可选 `W2A_API_KEY`（同时保护 `/studio/*` 与 `/v1`、`/admin/*`（`/health` 除外）、`/live` 的 REST 与 WebSocket）、`W2A_STORAGE_DIR`。
- 前端填口令：设了 `W2A_API_KEY` 后，SPA 左下角的「钥匙」按钮即为入口，填入后先拿 `/v1/models` 校验再保存（存 `localStorage`），此后 REST 统一带 `Authorization: Bearer`，WS 带 `?token=`。服务端没设口令时留空即可。
- `config.yaml`：provider 开关、首页地址、模型列表、**并发上限**、全局每分钟限流（非敏感）。跑批前先确认这里的 `max_concurrency` 与全局限流符合你能接受的频率。
- 每个 provider 的 `min_interval_sec`（秒，从上一次回复结束算起）：千问默认 3 秒，用于降低人机验证触发率；`0` 关闭。**过夜跑批别把它设成 0**。
- 人机验证：`W2A_AUTO_SLIDE=1` 自动处理（滑块直接拖）；`W2A_HUMAN_WAIT_SEC=60` 遇到只能人工做的图片点选验证码时，在有头浏览器里等人做完的最长秒数（`0` 不等）；`W2A_RISK_COOLDOWN=60` 验证失败后的风控冷却秒数。
- `content-types.yaml`（项目根）：文种与模板配置，见上面「文种全部配置化」。
- 写作业务库：`<W2A_STORAGE_DIR>/studio.sqlite`（默认 `storage/studio.sqlite`）。
- 底座自身的开关（真流式、SSE 心跳、跨域来源、热加载、页面绑定覆盖层、千问 HTTP 快路径）见 `docs/reverse-proxy.md`。

## 目录结构

```
app/
  main.py runtime.py config.py
  api/      studio_routes.py（62 个接口）  openai_routes.py  admin_routes.py  live_routes.py  conversation_routes.py  schemas.py
  studio/   schema.py store.py content_types.py assembler.py prompting.py tokenize.py chunker.py
            indexer.py retrieval.py summarizer.py graph.py consistency.py soft_rules.py
            scoring.py tasks.py jobs.py jsonutil.py            ← 写作业务全在这
  llm/      gateway.py        # 写作流程与 provider 解耦，复用下面的 registry 与限流
  core/     base.py modes.py registry.py sse.py store.py ratelimit.py errors.py blocked.py control_tokens.py
            auth.py alerts.py cut.py request_log.py ui_bindings.py
  browser/  playwright_pool.py session_manager.py stream_capture.py cdp_stream.py live_browser.py network_recorder.py ui_modes.py site_session.py
  providers/ _replay.py _browser_base.py  qwen.py doubao.py deepseek.py   ← 底座：驱动三家网页端
frontend/   Vue 3 + Vite（/studio /chat /admin /live，首页重定向到 /studio；构建产物 dist 由后端托管）
content-types.yaml   文种配置（kinds → scales → subtypes + 模板）
docs/       novel-studio-tech-design.md（设计与各里程碑实测）  m0-baseline.md  content-types.md  reverse-proxy.md
design-system/  本地生成，已 gitignore（写作台的界面规范）
web/        旧版静态页 chat.html admin.html live.html（前端未构建时兜底）
dev.py      frontend / backend / build / all
scripts/    smoke_studio.py  smoke_m1_tail.py  smoke_m2.py  smoke_m3.py  smoke_m4.py  smoke_soft_rules.py
            measure_baseline.py  shot_studio.py  cleanup_works.py  restore_backup.py  login.py  recon.py  check.ps1
tests/      pytest（全量 163 例；写作侧用 tmp_path 建临时库，不碰 storage/）
storage/    <浏览器 profile / 票据 / sessions.sqlite / studio.sqlite
            backups/nodes（删节前的 JSON 备份）/ backups/works（删作品前的整库副本），已 gitignore>
```

`scripts/` 下的验收脚本分两类，账号被风控时别看错哪类会花钱：

- **完全离线**（不起服务、不发起任何生成调用）：`smoke_soft_rules.py`（只读正文跑软规则）、`cleanup_works.py`（清书架 / 清孤儿行，默认试运行）、
  `restore_backup.py`（默认 `--list` 只看，`--name` 不带 `--yes` 也只打印会恢复什么）；
- **要先起服务**（跑本机 HTTP 接口）：`smoke_studio.py`（默认只读，`--generate` 才真生成）、`smoke_m3.py`（默认不调模型，`--llm` 才真实抽取）、
  `smoke_m1_tail.py` 与 `smoke_m2.py`（默认会真跑一次，`--skip-llm` 只验不调模型的部分）、`smoke_m4.py`（走队列，会生成）、
  `measure_baseline.py`（M0 基线实测，`--dry-run` 只看计划）、`shot_studio.py`（Playwright 截图，不发消息）。

## 常见问题

### 批量生成整队暂停

队列只在两种情况下主动停：**风控/登录失效** 与 **抽检连续两次判「重写」**。

- 前者处置与底座一样：`GET /live/debug/<key>` 看登录判定 → 去 `/live` 手动过一次验证 → 点「已验证·清除风控冷却」
  → `POST /studio/queue/resume`（页底队列条上也有按钮）。排查细则见 `docs/reverse-proxy.md`。
- 后者是止损阀，说明这一节自己都没通过抽检，先把正文改顺再 `resume`，否则后面每一节都在烂地上继续盖。

### 日志里反复出现「作品不存在」

那是 `delete_work()` 修好之前删掉的作品留下的孤儿 job 行：跑一次
`python -m scripts.cleanup_works --sweep-orphans --yes` 清掉（默认试运行，加 `--yes` 才动手，动手前先备份）。

### 正文里混进了站点的提示语或占位标记

两件事分开看：

- 站点的占位标记（`[[inline_action_reply_1]]`、`[[doc_common_card_1]]`）由后端在下发前剔除，见 `docs/reverse-proxy.md`；
- **给人看的错误提示绝不会落进正文**：`app/core/sse.py` 的 `DeltaEvent` 带 `hint` 标记，风控冷却、验证码等待、
  找不到输入框这类文案都标 `hint=True`，`app/llm/gateway.py` 见到就报错而不是当回答收下来。这一节会被标失败重排，
  而不是被一段提示语污染成正文。

### 账号被禁言 / 封禁

站点把输入框整个撤掉时，后端会认出「已被禁言 / 账号已被封禁 / 违反用户使用规范」这类文案，
带上「禁言至…」的期限报出来，并让**整队停下**——这种状态重试多少次都没用，继续跑只会白烧时间。
换账号或等解封后再 `resume`。

### 跑批跑一半，`/live` 里登录断了

写作与聊天共用同一个网页账号与**同一个标签页**：`/studio` 正在批量生成时去 `/live` 退出登录，写作会跟着掉进游客态。
反过来，`/live` 画面里弹出的验证码也是正在写这一节的请求触发的，直接在画面里做掉就行。

### 想少撞人机验证

把每个 provider 的 `min_interval_sec` 留着别设 0，一次别排太多节，`headless: false` 让验证码能看见也才能手动过；
千问 ticket 约 1 个月最能长期挂着，豆包与 DeepSeek 做不到免登录（登录态细节见 `docs/reverse-proxy.md`）。

## 已知限制

- **告警不阻塞定稿**：`PATCH /studio/nodes/{id}` 不查 `issues`，下拉框改成 `final` 就直接过（设计文档 9.5 已如实注明“没实现”）。
- **软规则只给 `info`**：不改正文也不扣分，到底算不算问题要人判；且只在作品 `checks` 里显式开着时才跑。
- **向量检索未接入**：换个说法的语义查询仍只能靠 jieba + FTS5 的关键词召回。
- 自动打分为 5 维，**不做多候选盲评**；`/transform` 的候选只并排列出来给人比。
- 模板里的 `target_words` 是软约束，实测偏差约 ±20%~40%。
- 删节点/删素材现在会把 `chunks_fts` / `index_units`（以及节点相关的 `summaries`）一起清掉；
  在这之前删下的历史残留用 `cleanup_works --sweep-orphans` 清（旧版只按 work_id 判，认不出「作品还活着、节点已没」的那些行）。
- **备份只在删除那一刻写，不是定时快照**：没删过的东西不会自己多一份副本（`storage/backups/`），
  也没做界面化的恢复入口——节点备份靠 `scripts/restore_backup.py`，整库副本靠停服务盖文件。
- **上下移只在同层**：不能把一节拖到另一个卷下面（要跨卷得自己剪切正文重排）。
- 生成吞吐受网页账号的限流与风控硬约束，串行跑一本长篇要按小时计；想要并发得自己解决账号问题。
- 底座（`/v1` OpenAI 接口、模式与文案校准）的限制另有 **十几条**，见 `docs/reverse-proxy.md` 的「已知限制（底座侧）」。

## 文档索引

- `docs/novel-studio-tech-design.md` — 设计文档，含各里程碑的实测记录与踩坑（当前 v2.0）
- `docs/m0-baseline.md` — M0 基线实测数据
- `docs/content-types.md` — 文种与模板配置约定
- `docs/reverse-proxy.md` — 底座：驱动三家网页端、`/v1` API 用法、风控排查细则

## 免责声明

本项目为技术学习与个人效率工具，使用者需自行承担违反平台条款及账号受限的风险。请勿用于商业用途或对外提供服务。
