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

开发时可以直接 `python dev.py all`（后端 :8000 + Vite :5273，热更新）；`build` / `backend` / `frontend` 三个子命令单独起。
改完代码一键自检：`powershell -ExecutionPolicy Bypass -File scripts\check.ps1`（ruff → pytest → 前端构建）。
**不想每次敲命令** → 直接跳到下面「桌面版（双击启动）」，一条脚本装完给你桌面上一个图标。

**开写之前先把账号登录上**：打开控制台 <http://127.0.0.1:8000/admin?tab=live>（旧 `/live` 会重定向过去）→ 选站点 → **连接** → 在实时画面里直接登录
（扫码/密码/滑块）→ **检测登录**。登录态落在持久化 profile 里长期复用，失效时管理面板会提示。
然后去 <http://127.0.0.1:8000/studio>（站点首页也跳这里）建作品。

## 桌面版（双击启动）

不想每次敲命令，就装桌面壳：桌面上多一个 **Novel Studio** 图标，双击起、关窗停。

```powershell
# 一次装好：venv + 两份依赖 + chromium 内核 + 前端构建 + .env + exe + 桌面快捷方式（幂等，可反复跑）
powershell -ExecutionPolicy Bypass -File scripts\setup_desktop.ps1
```

可选开关：`-SkipPlaywright`（内网拉不动浏览器内核时先跳过，推流/登录暂不可用）、`-NoExe`（不打 exe，快捷方式指向 `pythonw.exe desktop\launcher.py`，效果一样）、
`-NoShortcut`（只装环境不动桌面）、`-Rebuild`（已有 `frontend/dist` 与 exe 也重打一遍）。默认跳过这两步是有原因的：
`npm ci` 会先删空 `node_modules`，只要还有一个 node 进程开着 `rolldown` 的 `.node` 就报 EPERM，装机脚本得敢重跑就不能每次去动已经好的东西。

之后的日常只有两个动作：**双击图标** → 原生窗口（WebView2）打开 `/studio`；**关掉窗口** → 后端跟着停。
托盘图标右键还有：写作台 / 控制台 / 重启服务 / 打开日志目录 / 退出。重复双击不会开出第二个服务，
只会把已有窗口拽到前台（单实例互斥）。

几个容易踩的点，说清楚：

- **别为了省事双击 `desktop\launcher.py`**。Windows 会用**系统的** Python 接走它（.py 的文件关联），
  而壳依赖只装在 `.venv` 里，于是 `ModuleNotFoundError: No module named 'webview'`、窗口弹不出来。
  壳在起服务之前就会拦下这一种，并给出正确的解释器路径；真要用任意 Python 看界面，加 `--browser`。
- **关窗是真的收摊，不是拔电**。壳在 `127.0.0.1` 上开一个随机端口的控制口，向后端发 `STOP <一次性 token>`，
  uvicorn 走正常 lifespan：先停队列（**正在写的那一节退回 `queued`**，下次接着写），再关浏览器和库。
  收不干净（超时）才 `taskkill /T /F` 兜底。不用 `SIGINT` 是有原因的——Windows 上 `os.kill(pid, SIGINT)` 等价于硬杀，
  会留下 Chromium 孤儿进程和带 `SingletonLock` 的 profile。
- **端口上已经有健康后端就直接借用**。比如你自己跑过 `python dev.py backend`，壳不会再起一份，关窗口也绝不杀它；
  这种情况下托盘的「重启服务」会直接告诉你去那边重启（在这个壳里重启 = 同一端口撞死一个进程，却报「重启成功」）。
  `--stop` 也一样保守：状态文件里的 pid 若已经不再是本项目的 `.venv\Scripts\python.exe`（壳崩了、pid 被系统回收），
  它只报 `refused` 并给出那个 pid 现在跑的是什么，绝不对着无关进程 `taskkill /T /F`。
- **主题和 API Key 不会每次重填**。WebView2 的用户数据固定在 `storage/desktop/webview2/`（`localStorage` 落在那儿）。
- **exe 里不含后端**。打包只进壳（pywebview + pystray，onedir 约 36MB），运行时它去拉起 `.venv` 里的 Python。
  所以把 `desktop\dist\NovelStudio\` 拷去同事机器还得先在那台机器跑一次 `setup_desktop.ps1` 建环境。
  要重打：`powershell -ExecutionPolicy Bypass -File desktop\build.ps1`（改了 `desktop/` 下的代码记得加 `-Rebuild` 重跑装机脚本）。
- **改过 `*.ps1` 别把 UTF-8 BOM 弄丢**。PowerShell 5.1 把无 BOM 的文件当 ANSI（本机代码页 936）读，
  中文注释会被读坏，严重时整个脚本语法都解不过去（实测：字符串缺终止符 + 意外的标记「}」，脚本一行都不跑）。
  `.editorconfig` 已约束 `*.ps1` 用 `utf-8-bom`，测试 `test_powershell_scripts_with_chinese_have_utf8_bom` 会拦。

排查与命令行入口（打包后同样可用，`python` 换成 `.venv\Scripts\python.exe`）：

```powershell
python desktop\launcher.py --check     # 体检：项目目录 / venv / 后端依赖（问 .venv）/ 壳依赖 / 前端 / WebView2，逐项 ok-FAIL
python desktop\launcher.py --stop      # 停掉本机由壳起的后端（窗口没了但后端卡住时用）；认不准身份会 refused
python desktop\launcher.py --browser   # 原生窗口起不来（缺 WebView2 Runtime）就退回 Edge 应用模式
python desktop\launcher.py --root D:\x\novel-studio --path /admin --no-tray --debug
```

日志在 `storage/logs/` 下分两份：`desktop.log` 是后端（uvicorn + 应用）的输出，`desktop-shell.log` 是壳自己的事件
（起停、托盘、异常）。分开是必须的：后端子进程拿着一个追加模式的文件句柄，壳往同一个文件里插行会被它的后续输出盖掉。
exe 是无控制台程序，不把这些落盘就变成「双击没反应，什么都看不见」——`--check` 与 `--stop` 的输出也写进这份，
exe 上跑它们时没通过还会弹一个框。
地址与口令仍读 `.env`（`W2A_PORT` 默认 8000）；改完要重启壳才生效。桌面版只在 Windows 上实测过（Win10 LTSC），
非 Windows 会退化成「脚本模式 + 无托盘」，别指望 exe。

## 一条完整主线

写作台随时可以按 `Ctrl+Shift+F`（或顶栏「专注」）进入专注模式：左树与右栏收起，只留作品名、保存状态、正文与生成条，
`Esc` 或「退出专注」回来。它只动视线不动排版——字号行距仍归「排版」那一档管，切节也留在专注里。

1. 建作品（选 kind / subtype / 模板）→ 生成大纲 → 逐节生成（`POST /studio/works/{id}/pipeline` 走 SSE 看着它写，或 `POST …/batch` 落库入队关页面不管）
2. 每节生成前按桶装配上下文（S0–S8 + S_src；novel 默认 28,000 字符、article 18,000）。`POST /studio/context/preview` 只装配不花钱，能看到每桶占多少、丢了什么——**调 prompt 靠这个，不然就是盲调**
3. 生成后可选自动后处理：分块索引 → 三段式摘要（事件 / 变化 / 钩子）→ 图谱抽取 → 一致性检查
   （硬规则 9 条 + 软规则 5 条，最后一条「自我重复」专看跨节的字面复用：同一句在几节里重复、几节开头同一个起手式）→ 5 维打分
4. 全文拼装 `POST …/assemble`，导出 `GET …/export`（Markdown）
5. 选中任意段落可单独加工（重写 / 压缩 / 扩写 / 润色 / 翻译 / 拆分 / 合并 / 起标题，`POST /studio/transform`），多候选横向比较；
   「起标题」给 5 个备选（每个 ≤20 字，禁「震惊/必看/一文读懂」那套），点一行就回填到**本节标题**或**作品标题**
6. 下方框子那句提示词**留空也能生成**：单节点「生成」与「大纲 / 整篇 / 排队」走同一条指令口径
   （`prompting.node_instruction`：本节标题 / 本节要点 / 篇幅 / 只输出本节正文），右栏「上下文」显示的就是真要发出去的那一份
7. 作品标题与主旨建完还能改：点编辑器顶部的作品名就地编辑（`PATCH /studio/works/{id}`）；主旨进 S2，改完下一节就生效
8. 右栏「素材」：拖入 `.txt / .md` 可一次导多条（导入即建索引，标题留空取文件名或首行）；生成时**按本节标题与提示词检索素材**，
   不再固定取最新 5 条的开头。单条点开看全文、一键「抽事实卡」（自动带上出处），列表上的「已用 N 次」只在它真进了 prompt 时才加
9. **发公众号这一下**：头部「公众号」打开预览（375px 手机宽度，就是导出的那一份），点「复制到公众号」直接带走格式，
   粘进编辑器不用再删井号；同一页给发布前检查（段落过长、中文里混半角标点、外链、套话开头、通篇不加粗、摘要没填、风险词）
   与配图清单，摘要可以让 AI 写（≤120 字，落库），还能换 6 个标题、写 3 条转发语、切三套版式（纸张 / 极简 / 暖色）、
   补一段文末关注引导并自动带上「往期推荐」；正文里写「[图：画面描述]」就会变成导出时的配图占位
10. **文风**：右栏「文风」页可以给这一篇定人称 / 时态 / 语气 / 禁忌（三个预设一键填），也能贴几篇自己写过的旧文——
    本地统计句长、段长、口头禅、人称与标点，生成一段「文风画像」跟着 S1 进每次 prompt（不占素材与检索预算，不调模型）。
    发出去的文章在控制台「数据」里记一条台账（日期 / 链接 / 阅读 / 在看），跨作品汇总也在那一页
11. **作品多了也找得到**：书架有搜索框（标题 / 主旨即时筛）与「在写 / 已归档」两个视图；归档只改 `works.status`，
    正文、版本、索引都不动。跨作品搜正文与素材按 `Ctrl+K`（或书架上的「搜正文」），结果分「作品」与「正文片段」两组，
    点一条直接跳到那一篇的那一节；**改名换姓按 `Ctrl+H`**：全作品查找替换先看预览（哪节、几处、字数怎么变），
    看过再动手，替换前先把旧文存进版本表，术语同名跟着改，撞上已有术语只给一句警告、不毁整批
12. **写作手感**：书架顶部一条写作日历（今天手写字数 / 连续天 / 近 14 天柱状），下面是「灵感箱」——一句话想法先记着，
    「用它开写」只把它填进新建表单；生成结果可以逐段勾选后再采用，编辑器顶部有「本节 / 目标」进度
13. **结构与资料可维护**：左树能折叠（写完的卷先收起来，状态按作品记住；跳到被折叠的节会自动展开祖先），
    树头「导入」能把旧稿一次搬进来——粘贴或拖 `.txt / .md`，按「第X章 / ## 标题」或空行拆节，看过预览再逐节建；
    右栏的素材、事实卡、术语都能就地改，素材正文改完会重建这一条的检索索引（不改的话生成还在按旧文字找资料）；
    左树上把一章**拖到另一卷**就行——只搬章与节（卷本身不拖），落点只有目标卷的末尾，松手后全书章号按树序重排成 1..N，
    摘要、分块与图谱里的章号一起跟过去
14. **定稿不是一键过**：状态切「定稿」前先看这一节还有几条没处理的硬告警（`GET /studio/nodes/{id}/final-gate`），
    「死者说话」「道具已毁」这类必然错的先摆出来，点「仍然定稿」才放行（`PATCH` 不带 `force` 回 409 与同一份清单）。
    只拦本节 + `error` + 未处理：warn/info、别的节与全书级告警都不挡，定稿也不顺手帮你消告警
15. **换机 / 借给人看**：书架「导出整库」打成一个 JSON（每本一个自足的包：树、全部历史版本、素材、事实、术语、设定卡与图谱），
    新机器上「导入」即重建——id 全部重发、引用跟着改，同一份包导两次得到两本独立的书；作品卡悬停还能单本导出。
    摘要、分块、运行记录不进包（从正文派生），导入后重建索引，摘要按需重算
16. **钱花在哪儿、写得好不好**：控制台「数据」页有「生成成本与质量」——按任务、按作品、按最耗时的节看调用次数、失败数、
    总耗时与每次耗时、输出字数与均分，旁边挂着未处理的硬告警数。均分只算打过分的跑批，没评不按 0 分算

## 两种形态

| 形态 | 层级 | 长期记忆 | 专属能力 |
|---|---|---|---|
| `novel` 小说 | 卷 / 章 | 设定卡 + 实体/关系/事件/伏笔图谱 | 节拍卡、章纲；硬规则：死人复活 / 归属冲突（物品双持）/ 时间倒退 / 已毁地点 / 伏笔超期 |
| `article` 文档 | 节（单篇）/ 讲 → 节（系列） | 素材 / 事实卡 / 术语 | 模板大纲；硬规则：事实过期 / 缺引用 / 术语不统一 / 结构不完整 |

## 无人值守

单 worker 串行队列 + 优先级 + 幂等入队（`idem_key`）；重启把 `running` 退回 `queued` 自动续跑；
遇到风控或登录失效**整队暂停**；每 `triage_every` 节抽检一次，连续两次判「重写」就熔断停队。

写作台页底只有一行队列进度（排队 / 完成 / 约 X 分钟 / 当前在跑的那一节）；暂停与恢复（`POST /studio/queue/resume`）、
抽检判定、单条任务的重试与取消（`POST /studio/jobs/{id}/retry`，会把 attempts 清零）都在控制台「任务」（`/admin?tab=tasks`）里。
每一节用了哪个 model / provider、输入输出各多少字符、耗时与打分，都记在 `runs` 表里，成本可以回看
（用量是字符估算，不是真实 token）；控制台「数据」页把这些按任务 / 作品 / 节汇总成一张「生成成本与质量」表。

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
- **每一版都留得回来**。每次「采用」、生成落库、全作品替换都会存一个版本，来源标成 AI 草稿 / AI 润色 / 手改 / 批量替换
  （接口只认这四个值，乱填的当场 400；交互式润色落的那一版标 `ai_polish`，不再和「AI 草稿」混成一行；
  替换那一档叫 `bulk_replace`，它不刷高「今天写的字」），
  右栏「历史」页能列出、能跟当前正文并排比对、能点「用这一版」回滚。
  回滚前服务端会先把当下这份还没进版本表的正文存一版，所以点回滚不会吃掉你刚敲的几百字。
- **上下移会把章号一起带走**。树上的 ↑↓ 只跟同一个父节点下的紧邻兄弟互换；「第几章」这个数在库里存了十份
  （章节序、摘要序号、分块序、实体的登场/死亡章号、关系与事件与伏笔的章号、角色状态章号），
  交换时一起对调，否则「已死的角色在第 5 章出场」这类判定会跟着错位。
  跨卷拖拽不是对调而是整体重排：按树序把全书章号写成 1..N，同样那十列一起映射（先整体取负再翻正，
  避开 `UNIQUE(entity_id, ordinal)` 与关系唯一键的中间态）；映射外的章号（比如伏笔计划在尚未写到的第 100 章回收）原样留着。
  内容没变，所以不重算索引与摘要，只把库标脏。
- **删除前自动留备份**。删一节（含整棵子树与全部历史版本）先写一份 JSON 到 `storage/backups/nodes/`，
  删作品先在锁内取一份整库副本到 `storage/backups/works/`（`VACUUM INTO` 的一致快照，不是热拷文件）；
  **备份写不成就拒绝删除**，界面上只给「备份失败，已拒绝删除，详见服务端日志」这一句固定文案——
  `detail` 会原样显示出来，而失败原因里带着库和备份目录在本机的位置，那句只该进日志。
  找回一节：`python -m scripts.restore_backup --list` 看有哪些，`--name <文件>` 试运行，加 `--yes` 真恢复
  （原父节点还在就挂回去，没了就挂作品根；章号追加到末尾，再用 ↑↓ 挪回原位）。
  `--list` 与试运行**完全不碰数据库**，只读备份目录里的文件，所以服务还开着的时候也能放心跑；真恢复才开连接。
  整库副本只列不给脚本改：那种找回是停服务后把文件盖回 `storage/studio.sqlite`。
  节点备份留最近 20 份、整库副本留最近 5 份，删的时候顺手清理（清理失败只写日志，不会反过来把删除卡住）。
  清理只认 `*.json` / `*.sqlite` 这一种名单：写临时名留下的半截 `.part` 不占名额——它 mtime 最新，
  算进去就会把「最近一份真备份」挤掉，而人要用时想要的恰恰是那一份。
- **备份保的是稿子，不是记忆**。那一份 JSON 只带走正文与全部历史版本：摘要、告警、生成记录、索引都不在里面。
  所以恢复之后要把这一节的摘要重算一次；恢复这一步会顺手把上游的卷摘要与全书摘要判过期、并把库标脏，
  右栏的「N 节摘要待重算」能看见它们，不会拿「没有这一节」那一版的摘要继续往下注入。

## 检查规则

- **硬规则 9 条**（小说 5 + 文档 4，即上表「专属能力」那串）：结构上必然错，报 `error/warn` 并参与「设定合规」扣分；
- **软规则 5 条**（`address_drift` 称呼漂移 / `setting_conflict` 设定冲突（目前只判年龄）/ `absolute_claim` 绝对化表述 / `number_unsourced` 数字无出处 / `phrase_repeat` 自我重复）：
  **零 LLM 调用**，一律 `info`、不扣分、不阻塞定稿，每节每规则封顶 3 条，措辞永远是「请确认」——改判交给人。
  哪几条开在每个文种的 `checks` 里自己选：小说默认开前两条，文档按子类只开绝对化表述 / 数字无出处；
  `phrase_repeat` 只有整本检查跑得出来（跨节才看得出复用），小说与系列文档都默认开着：5 字起的字面复用落在 ≥3 节
  才报，人物/道具/地点/组织的名字、术语、节标题一律先排掉——真实语料上线的口径是「先验证不报再接线」。

告警在右栏分「硬 / 待确认」两组渲染，点「定位」会高亮正文对应片段。

## 图谱

实体 / 关系 / 事件 / 伏笔从正文里抽出来存库，除了列表还能开力导向视图（虚线圈 = 已死/已毁，虚线边 = 已过期关系）。
弹层头部可切「关系 / 时间轴」：时间轴按节把事件（谁参与、故事内时间、重要度）与人物状态排开，点一行跳回那一节；
还没抽过的作品在那里只有一句「先在右栏点『抽本节』」，不是空白。
抽取要真调模型（`smoke_m3.py --llm` 是它的验收入口），右栏「图谱」那页会给出实体 / 关系 / 伏笔的计数。

## 文种全部配置化

`content-types.yaml`（`kinds → scales → subtypes` 三层合并；预算、启用的上下文桶、层级、`checks`/`checks_append`），
**新增文种不改代码**；改完调 `POST /studio/content-types/reload` 立即生效，前端下拉框的数据源是 `GET /studio/content-types`。

同文件的顶层 `channels` 是与文种**正交**的第二个维度：文种管「写什么」，渠道管「发到哪」（公众号要短段与配图位，
知乎要结论先行，小红书要开头三行给利益点）。「历史 + 公众号」不再需要在两者间二选一。渠道的写法在 S1 里单独成段、
带「平台要求（公众号）：」前缀——它说「发到哪」，文体要求说「写什么领域」，拼成一句时模型会拿其中一条把另一条顶掉；
`label`/预算/层级仍归文种管，`checks_append` 与文种合并去重，字数与段落节奏则由渠道决定。第三个维度是文风
（`works.style_json` 与文风画像），三者合起来才是「这一篇怎么写」。

启动时按它覆盖库内内置模板（`is_builtin=1`），用户自建模板不动；写坏了不会让服务起不来（解析失败回落内置兜底，接口返回里带 `error`）。
**模板库在控制台的「配置」页维护**（新建 / 克隆 / 设为默认 / 收藏 / 删除 / 导入导出；内置行只读，要改先复制一份），
写作台右栏「模板」页可以换模板、换渠道、内联改骨架，也能把当前这篇已有的节沉淀成一套新骨架。
新作品的模板按「`kind+subtype+channel` 默认 → `kind+subtype` 默认 → `kind+channel` 默认 → YAML 出厂默认 → 该文种任意一个 → 无」六级链自动挑，
也可以指定这套或明确「不套模板」。约定见 `docs/content-types.md`。

## 数据与清理

业务数据落 `storage/studio.sqlite`，与底座对话用的 `sessions.sqlite` 分开；表结构见 `app/studio/schema.py`。

`DELETE /studio/works/{id}` 会连作品自己的数据一起清，但 `jobs`/`runs`/`index_units`/`chunks_fts` 四张表不受外键保护
（FTS5 是虚拟表，接不到级联），由删除逻辑手动清；删单节、删素材同样会带走 `chunks_fts` / `index_units`（节点还多清 `summaries`，
否则已删章节会以摘要形式继续进 prompt）。历史残留用：

```powershell
python -m scripts.cleanup_works --sweep-orphans    # 默认试运行，加 --yes 才动手，动手前自动备份
```

清书架也走这个脚本（同样默认试运行）。

删除本身也留退路：删节/删卷写 `storage/backups/nodes/*.json`（正文 + 全部历史版本），删作品在锁内取
`storage/backups/works/*.sqlite` 整库副本（`VACUUM INTO` 的一致快照）；备份写不成就拒绝删除。
备份里只有正文与版本，摘要/告警/生成记录不在里面——找回的那一节要重算摘要，见上面「改完稿子会发生什么」。
找回入口：`python -m scripts.restore_backup --list`（只看，不碰数据库）。脚本先按与恢复那侧同一套判据
（`store.check_node_bundle`）校验备份结构，才敢说「restore 没成功 = 那本书整本删了」；判据写两套就会
把空 `nodes`、没写 work_id 这类坏备份报成「书没了」，那是另一种谎话。

每天还有一份**整库自动快照**：服务起来先看一眼，之后每 30 分钟看一眼，当天第一次真改过正文就 `VACUUM INTO`
一份到 `storage/backups/snapshots/`，留最近 7 份。空库不占名额、没改过字不产重复副本、今天取过不再取第二份；
写不成只进日志，不拦任何一次保存。控制台「数据」页也能手动按一份（`POST /studio/backups/snapshot`），
手动那份不顶掉当天的自动那份——早上按过、晚上崩了也该有退路。

## 界面规范

深色「墨与金」：唯一强调色 `#D9A45B`、正文衬线 18px / 行高 1.9、编辑区宽度封顶 646px。
完整规范由 `ui-ux-pro-max` 生成在本地 `design-system/`（已 gitignore，不入库，随时可重新生成）。

**默认字号 18 是有来由的**：Windows 的宋体在 ≤17px 走内嵌点阵——实测「半覆盖边缘像素」
在 15/16/17px 恒为 0（完全没有抗锯齿），18px 起才到 2.88%；旧默认 17px 正好卡在点阵档里。
字体栈也把西文挑到前面（`Georgia, Cambria` 打头，中文按「思源宋体 → 华文中宋 → 宋体」装了才用），
否则数字与拉丁字母会交给宋体自带那套窄西文，中英混排时基线与字宽都打架。
（`-webkit-font-smoothing: antialiased` 在 Windows 上实测逐像素无影响，它是 macOS 那条，
别再往它身上找原因。）

写作台顶栏「排版」里字体有 衬线 / 楷体 / 无衬线 / 等宽 四档（`--w-serif|kai|sans|mono`，
栈本体只在 `tailwind.config.js` 一处），偏好只存 `localStorage`，不动正文一个字。
AI 草稿预览与流水线预览也吃当前字体档，字号给到 15px / 14px：这两块区域高度是钉死的，
抬不进 18px 那一档，但再往下压（原来是 13.5 / 12.5）就又回到点阵里了。

## 配置

- `.env`（由 `.env.example` 复制）：`W2A_HOST/PORT`、可选 `W2A_API_KEY`（同时保护 `/studio/*` 与 `/v1`、`/admin/*`（`/health` 除外）、`/live` 的 REST 与 WebSocket）、`W2A_STORAGE_DIR`。
- 前端填口令：设了 `W2A_API_KEY` 后，SPA 左下角的「钥匙」按钮即为入口，填入后先拿 `/v1/models` 校验再保存（存 `localStorage`），此后 REST 统一带 `Authorization: Bearer`，WS 带 `?token=`。服务端没设口令时留空即可。
- `config.yaml`：provider 开关、首页地址、模型列表、**并发上限**、全局每分钟限流（非敏感）。跑批前先确认这里的 `max_concurrency` 与全局限流符合你能接受的频率。
- 每个 provider 的 `min_interval_sec`（秒，从上一次回复结束算起）：千问默认 3 秒，用于降低人机验证触发率；`0` 关闭。**过夜跑批别把它设成 0**。
- 人机验证：`W2A_AUTO_SLIDE=1` 自动处理（滑块直接拖）；`W2A_HUMAN_WAIT_SEC=60` 遇到只能人工做的图片点选验证码时，在有头浏览器里等人做完的最长秒数（`0` 不等）；`W2A_RISK_COOLDOWN=60` 验证失败后的风控冷却秒数。
- `content-types.yaml`（项目根）：文种、渠道与模板配置，见上面「文种全部配置化」。
- 写作业务库：`<W2A_STORAGE_DIR>/studio.sqlite`（默认 `storage/studio.sqlite`）。
- 桌面壳依赖：`requirements-desktop.txt`（pywebview / pystray / pillow / pyinstaller），与后端依赖分开；
  `scripts\setup_desktop.ps1` 会两份一起装。壳往后端传 `W2A_CONTROL_PORT` / `W2A_CONTROL_TOKEN`（随机、一次性，停机协议用），
  这两个不需要人写进 `.env`。
- 底座自身的开关（真流式、SSE 心跳、跨域来源、热加载、页面绑定覆盖层、千问 HTTP 快路径）见 `docs/reverse-proxy.md`。

## 目录结构

```
app/
  main.py runtime.py config.py
  api/      studio_routes.py（98 个接口）  openai_routes.py  admin_routes.py  live_routes.py  conversation_routes.py  schemas.py
  studio/   schema.py store.py content_types.py assembler.py prompting.py tokenize.py chunker.py
            indexer.py retrieval.py summarizer.py graph.py consistency.py soft_rules.py
            scoring.py tasks.py jobs.py jsonutil.py
            wechat.py（公众号 HTML）publish.py（发布前检查）voice.py（文风画像）
                                                            ← 写作业务全在这
  llm/      gateway.py        # 写作流程与 provider 解耦，复用下面的 registry 与限流
  core/     base.py modes.py registry.py sse.py store.py ratelimit.py errors.py blocked.py control_tokens.py
            auth.py alerts.py cut.py request_log.py ui_bindings.py
  browser/  playwright_pool.py session_manager.py stream_capture.py cdp_stream.py live_browser.py network_recorder.py ui_modes.py site_session.py
  providers/ _replay.py _browser_base.py  qwen.py doubao.py deepseek.py   ← 底座：驱动三家网页端
frontend/   Vue 3 + Vite（写作台 `/studio`（含右栏「问一问」）/ 控制台 `/admin?tab=状态|浏览器|日志|任务|数据|配置`，
            旧 `/chat`、`/live` 链接会重定向过去；首页重定向到 /studio；构建产物 dist 由后端托管）
desktop/    桌面壳（不含后端）：launcher.py 窗口+托盘 / serve.py 后端入口带控制口 / backend.py 起停与三级兜底
            control.py 停机协议 / config.py 读 .env 与找项目根 / win32.py 互斥·消息框·杀进程树
            make_icon.py + assets/novel-studio.ico / NovelStudio.spec + build.ps1（打 exe，产物 desktop/dist 已 gitignore）
content-types.yaml   文种与渠道配置（kinds → scales → subtypes + channels + 模板）
risk-words.yaml      公众号风险词（发布前检查用，改完不用重启）
docs/       novel-studio-tech-design.md（设计与各里程碑实测）  m0-baseline.md  content-types.md  reverse-proxy.md
design-system/  本地生成，已 gitignore（写作台的界面规范）
web/        旧版静态页 chat.html admin.html live.html（前端未构建时兜底）
dev.py      frontend / backend / build / all
scripts/    smoke_studio.py  smoke_m1_tail.py  smoke_m2.py  smoke_m3.py  smoke_m4.py  smoke_soft_rules.py
            measure_baseline.py  shot_studio.py  cleanup_works.py  restore_backup.py  login.py  recon.py  check.ps1
            setup_desktop.ps1（桌面版一键装机 + 快捷方式）
tests/      pytest（全量 373 例，含桌面壳 40 例；写作侧用 tmp_path 建临时库，不碰 storage/）
storage/    <浏览器 profile / 票据 / sessions.sqlite / studio.sqlite
            backups/nodes（删节前的 JSON 备份）/ backups/works（删作品前的整库副本）
            backups/snapshots（每日整库快照，留 7 份），已 gitignore>
```

`scripts/` 下的验收脚本分两类，账号被风控时别看错哪类会花钱：

- **完全离线**（不起服务、不发起任何生成调用）：`smoke_soft_rules.py`（只读正文跑软规则）、`cleanup_works.py`（清书架 / 清孤儿行，默认试运行）、
  `restore_backup.py`（默认 `--list` 只看，`--name` 不带 `--yes` 也只打印会恢复什么——这两条连数据库都不开，
  服务正在跑也能查；只有 `--yes` 才真往库里写）；
- **要先起服务**（跑本机 HTTP 接口）：`smoke_studio.py`（默认只读，`--generate` 才真生成）、`smoke_m3.py`（默认不调模型，`--llm` 才真实抽取）、
  `smoke_m1_tail.py` 与 `smoke_m2.py`（默认会真跑一次，`--skip-llm` 只验不调模型的部分）、`smoke_m4.py`（走队列，会生成）、
  `measure_baseline.py`（M0 基线实测，`--dry-run` 只看计划）、`shot_studio.py`（Playwright 截图，不发消息）。

## 常见问题

### 批量生成整队暂停

队列只在两种情况下主动停：**风控/登录失效** 与 **抽检连续两次判「重写」**。

- 前者处置与底座一样：`GET /live/debug/<key>` 看登录判定 → 去控制台「浏览器」（`/admin?tab=live`）手动过一次验证 → 点「已验证·清除风控冷却」
  → `POST /studio/queue/resume`（控制台「任务」里也有按钮）。排查细则见 `docs/reverse-proxy.md`。
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

- **定稿闸门只看本节 + error**：warn / info、别的节与全书级告警（`node_id` 为空）都不挡；确认「仍然定稿」只是一次布尔放行
  （`force=true`），**不记原因**——原设计里「忽略并记原因」没有落库，要留痕得另加一列。
- **软规则只给 `info`**：不改正文也不扣分，到底算不算问题要人判；且只在作品 `checks` 里显式开着时才跑。
- **向量检索未接入**：换个说法的语义查询仍只能靠 jieba + FTS5 的关键词召回。
- 自动打分为 5 维，**不做多候选盲评**；`/transform` 的候选只并排列出来给人比。
- 模板里的 `target_words` 是软约束，实测偏差约 ±20%~40%。
- 删节点/删素材现在会把 `chunks_fts` / `index_units`（以及节点相关的 `summaries`）一起清掉；
  在这之前删下的历史残留用 `cleanup_works --sweep-orphans` 清（旧版只按 work_id 判，认不出「作品还活着、节点已没」的那些行）。
- **备份能救「删错了」，救不了「这一小时刚写的」**：删节/删作品各有独立副本（`storage/backups/`），整库另有
  每日快照（留 7 份），而快照是当天的一份、不是逐次改动的——今天早些时候的正文靠的是版本表（右栏「历史」），
  不是备份。整库副本没有界面化恢复入口：节点备份走 `scripts/restore_backup.py`，整库副本靠停服务把文件盖回
  `storage/studio.sqlite`；库文件彻底损坏时能用的是 `backups/snapshots/` 里那一份。
- **迁移包只带「写下的东西」**：正文、版本、素材、事实、术语、设定卡与图谱在包里；摘要、分块、运行记录不在——导入后重建索引，
  摘要要重算一次。导入只做「新增」：id 全部重发，同一份包导两次得到两本独立的书；整库一次最多 200 本、单本上限 2 万个节点。
- **拖拽只搬叶子、只落末尾**：章与节可以拖进任意一卷的末尾（或顶层），卷本身不拖、也不能插到某一节之前
  （那要另配一套插入交互与半自动排序）；跨作品拖拽不做。
- 生成吞吐受网页账号的限流与风控硬约束，串行跑一本长篇要按小时计；想要并发得自己解决账号问题。
- 底座（`/v1` OpenAI 接口、模式与文案校准）的限制另有 **十几条**，见 `docs/reverse-proxy.md` 的「已知限制（底座侧）」。

## 文档索引

- `docs/novel-studio-tech-design.md` — 设计文档，含各里程碑的实测记录与踩坑（当前 v2.10）
- `docs/m0-baseline.md` — M0 基线实测数据
- `docs/content-types.md` — 文种、渠道与模板配置约定
- `docs/reverse-proxy.md` — 底座：驱动三家网页端、`/v1` API 用法、风控排查细则

## 免责声明

本项目为技术学习与个人效率工具，使用者需自行承担违反平台条款及账号受限的风险。请勿用于商业用途或对外提供服务。
