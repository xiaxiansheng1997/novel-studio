"""写作业务库的表结构。

与对话库（`storage/sessions.sqlite`）分离，避免业务写入干扰对话。
设计主线：作品（works）之下一棵节点树（nodes），小说与文档共用，
差异由 `kind` / `scale` 决定——见 docs/novel-studio-tech-design.md 第 5 章。
"""
from __future__ import annotations

SCHEMA = """
PRAGMA foreign_keys = ON;

-- ============ 作品与节点 ============

CREATE TABLE IF NOT EXISTS templates (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,               -- novel / article
    subtype TEXT DEFAULT '',          -- tech/history/politics/lifestyle/report/course
    name TEXT NOT NULL,
    outline_json TEXT NOT NULL,       -- [{heading, prompt, target_words}]
    style_prompt TEXT DEFAULT '',
    checks_json TEXT DEFAULT '[]',
    is_builtin INTEGER DEFAULT 0,
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS works (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,               -- novel / article
    scale TEXT DEFAULT 'single',      -- article 专用：single / series
    subtype TEXT DEFAULT '',
    title TEXT NOT NULL,
    premise TEXT DEFAULT '',
    status TEXT DEFAULT 'active',     -- active / paused / archived
    template_id TEXT REFERENCES templates(id) ON DELETE SET NULL,
    target_words INTEGER DEFAULT 0,
    style_json TEXT DEFAULT '{}',
    meta_json TEXT DEFAULT '{}',
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_works_kind ON works(kind, status, updated_at DESC);

CREATE TABLE IF NOT EXISTS nodes (
    id TEXT PRIMARY KEY,
    work_id TEXT NOT NULL REFERENCES works(id) ON DELETE CASCADE,
    parent_id TEXT REFERENCES nodes(id) ON DELETE CASCADE,
    path TEXT NOT NULL,
    depth INTEGER NOT NULL,
    ordinal INTEGER,                  -- 可读单元全序；容器节点为 NULL
    kind TEXT NOT NULL,               -- volume / chapter / section
    title TEXT NOT NULL,
    content TEXT DEFAULT '',
    status TEXT DEFAULT 'draft',      -- draft / review / final
    word_count INTEGER DEFAULT 0,
    content_hash TEXT DEFAULT '',
    summary TEXT DEFAULT '',
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_nodes_path ON nodes(work_id, path);
CREATE INDEX IF NOT EXISTS idx_nodes_ordinal ON nodes(work_id, ordinal);
CREATE INDEX IF NOT EXISTS idx_nodes_parent ON nodes(parent_id, path);

CREATE TABLE IF NOT EXISTS node_versions (
    id TEXT PRIMARY KEY,
    node_id TEXT NOT NULL REFERENCES nodes(id) ON DELETE CASCADE,
    version_no INTEGER NOT NULL,
    content TEXT NOT NULL,
    source TEXT NOT NULL,             -- ai_draft / ai_polish / human
    run_id TEXT,
    created_at REAL NOT NULL,
    UNIQUE(node_id, version_no)
);

-- ============ 单篇四件套：素材 / 事实 / 术语 / 模板已在上方 ============

CREATE TABLE IF NOT EXISTS sources (
    id TEXT PRIMARY KEY,
    work_id TEXT NOT NULL REFERENCES works(id) ON DELETE CASCADE,
    kind TEXT DEFAULT 'note',         -- note / web / clip / file
    title TEXT DEFAULT '',
    url TEXT DEFAULT '',
    content TEXT NOT NULL,
    published_at TEXT DEFAULT '',
    fetched_at REAL,
    used INTEGER DEFAULT 0,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sources_work ON sources(work_id, created_at DESC);

CREATE TABLE IF NOT EXISTS facts (
    id TEXT PRIMARY KEY,
    work_id TEXT NOT NULL REFERENCES works(id) ON DELETE CASCADE,
    statement TEXT NOT NULL,
    source_id TEXT REFERENCES sources(id) ON DELETE SET NULL,
    confidence REAL DEFAULT 1.0,
    valid_until TEXT DEFAULT '',
    cited_node_id TEXT,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_facts_work ON facts(work_id);

CREATE TABLE IF NOT EXISTS terms (
    id TEXT PRIMARY KEY,
    work_id TEXT NOT NULL REFERENCES works(id) ON DELETE CASCADE,
    term TEXT NOT NULL,
    definition TEXT DEFAULT '',
    preferred TEXT DEFAULT '',
    forbidden TEXT DEFAULT '[]',
    created_at REAL NOT NULL,
    UNIQUE(work_id, term)
);

-- ============ 检查结果 / 生成记录 / 任务 ============

CREATE TABLE IF NOT EXISTS issues (
    id TEXT PRIMARY KEY,
    work_id TEXT NOT NULL REFERENCES works(id) ON DELETE CASCADE,
    node_id TEXT REFERENCES nodes(id) ON DELETE CASCADE,
    rule TEXT NOT NULL,
    severity TEXT DEFAULT 'warn',     -- info / warn / error
    message TEXT NOT NULL,
    evidence TEXT DEFAULT '',
    location TEXT DEFAULT '',
    resolved INTEGER DEFAULT 0,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_issues_node ON issues(node_id, resolved);

CREATE TABLE IF NOT EXISTS runs (
    id TEXT PRIMARY KEY,
    work_id TEXT NOT NULL,
    node_id TEXT,
    task_type TEXT NOT NULL,
    model TEXT DEFAULT '',
    provider TEXT DEFAULT '',
    prompt_chars INTEGER DEFAULT 0,
    output_chars INTEGER DEFAULT 0,
    prompt_hash TEXT DEFAULT '',
    variant INTEGER DEFAULT 0,
    score REAL,
    status TEXT DEFAULT 'running',    -- running / ok / failed / interrupted
    error TEXT DEFAULT '',
    elapsed_ms INTEGER DEFAULT 0,
    started_at REAL NOT NULL,
    finished_at REAL
);

CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    work_id TEXT NOT NULL,
    node_id TEXT,
    task_type TEXT NOT NULL,
    payload_json TEXT DEFAULT '{}',
    status TEXT DEFAULT 'queued',
    priority INTEGER DEFAULT 5,
    attempts INTEGER DEFAULT 0,
    max_attempts INTEGER DEFAULT 2,
    idem_key TEXT UNIQUE,
    last_error TEXT DEFAULT '',
    scheduled_at REAL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);

-- ============ 图谱（M3，仅 novel 写入） ============

-- 作者维护的「圣经」，权威事实（优先级高于抽取结果）
CREATE TABLE IF NOT EXISTS cards (
    id TEXT PRIMARY KEY,
    work_id TEXT NOT NULL REFERENCES works(id) ON DELETE CASCADE,
    kind TEXT NOT NULL,               -- character/location/item/faction/rule
    name TEXT NOT NULL,
    aliases TEXT DEFAULT '[]',
    summary TEXT DEFAULT '',
    data_json TEXT DEFAULT '{}',
    pinned INTEGER DEFAULT 0,
    updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_cards_work ON cards(work_id, kind);

CREATE TABLE IF NOT EXISTS entities (
    id TEXT PRIMARY KEY,
    work_id TEXT NOT NULL REFERENCES works(id) ON DELETE CASCADE,
    kind TEXT NOT NULL,               -- character/location/item/faction/rule/event/foreshadow
    name TEXT NOT NULL,
    canonical TEXT NOT NULL,          -- 规范化名，用于对齐
    aliases TEXT DEFAULT '[]',
    card_id TEXT REFERENCES cards(id) ON DELETE SET NULL,
    first_ordinal INTEGER DEFAULT 0,
    status TEXT DEFAULT 'alive',      -- alive/dead/destroyed/missing
    status_ordinal INTEGER DEFAULT 0,
    UNIQUE(work_id, canonical)
);
CREATE INDEX IF NOT EXISTS idx_entities_work ON entities(work_id, kind);

CREATE TABLE IF NOT EXISTS relations (
    id TEXT PRIMARY KEY,
    work_id TEXT NOT NULL REFERENCES works(id) ON DELETE CASCADE,
    src_id TEXT NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
    dst_id TEXT NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
    rel_type TEXT NOT NULL,           -- 师徒/亲属/敌对/同盟/隶属/持有/位于/参与/因果
    since_ordinal INTEGER DEFAULT 0,
    until_ordinal INTEGER,            -- NULL = 仍有效
    confidence REAL DEFAULT 1.0,
    evidence_ordinal INTEGER,
    UNIQUE(src_id, rel_type, dst_id, since_ordinal)
);
CREATE INDEX IF NOT EXISTS idx_rel_src ON relations(work_id, src_id);
CREATE INDEX IF NOT EXISTS idx_rel_dst ON relations(work_id, dst_id);

CREATE TABLE IF NOT EXISTS events (
    id TEXT PRIMARY KEY,
    work_id TEXT NOT NULL REFERENCES works(id) ON DELETE CASCADE,
    ordinal INTEGER NOT NULL,
    seq_in_unit INTEGER DEFAULT 0,
    summary TEXT NOT NULL,
    participants TEXT DEFAULT '[]',   -- JSON: 实体 id 数组
    location_id TEXT,
    story_time TEXT DEFAULT '',       -- 故事内时间，规范化字符串
    importance INTEGER DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_events_work ON events(work_id, ordinal, seq_in_unit);

CREATE TABLE IF NOT EXISTS entity_states (
    id TEXT PRIMARY KEY,
    work_id TEXT NOT NULL REFERENCES works(id) ON DELETE CASCADE,
    entity_id TEXT NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
    ordinal INTEGER NOT NULL,
    state_json TEXT NOT NULL,
    UNIQUE(entity_id, ordinal)
);

CREATE TABLE IF NOT EXISTS foreshadows (
    id TEXT PRIMARY KEY,
    work_id TEXT NOT NULL REFERENCES works(id) ON DELETE CASCADE,
    planted_ordinal INTEGER NOT NULL,
    planned_payoff_ordinal INTEGER,
    resolved_ordinal INTEGER,
    content TEXT NOT NULL,
    status TEXT DEFAULT 'open',       -- open/resolved/abandoned
    importance INTEGER DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_foreshadow_work ON foreshadows(work_id, status);

-- ============ 索引与摘要（M2） ============

CREATE TABLE IF NOT EXISTS chunks (
    id TEXT PRIMARY KEY,
    work_id TEXT NOT NULL REFERENCES works(id) ON DELETE CASCADE,
    source_kind TEXT NOT NULL DEFAULT 'body',
    node_id TEXT REFERENCES nodes(id) ON DELETE CASCADE,
    source_id TEXT REFERENCES sources(id) ON DELETE CASCADE,
    ordinal INTEGER DEFAULT 0,
    seq_in_unit INTEGER DEFAULT 0,
    text TEXT NOT NULL,
    text_tok TEXT NOT NULL,
    char_start INTEGER DEFAULT 0,
    char_end INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_chunks_work ON chunks(work_id, source_kind, ordinal);
CREATE INDEX IF NOT EXISTS idx_chunks_node ON chunks(node_id);
CREATE INDEX IF NOT EXISTS idx_chunks_source ON chunks(source_id);

-- FTS5 虚拟表：单独建而不是 external content，避免正文表变动时触发器不一致。
-- text_tok 是 jieba 预分词后的空格串（unicode61 不切中文，见 app/studio/tokenize.py）
CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
    chunk_id UNINDEXED,
    work_id UNINDEXED,
    text_tok,
    tokenize = 'unicode61'
);

-- 索引台账：按 ref 记录内容哈希，实现增量重建（只重建变过的节点/素材）
CREATE TABLE IF NOT EXISTS index_units (
    ref_kind TEXT NOT NULL,          -- node / source
    ref_id TEXT NOT NULL,
    work_id TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    chunk_count INTEGER DEFAULT 0,
    updated_at REAL NOT NULL,
    PRIMARY KEY (ref_kind, ref_id)
);
CREATE INDEX IF NOT EXISTS idx_index_units_work ON index_units(work_id, ref_kind);

CREATE TABLE IF NOT EXISTS summaries (
    id TEXT PRIMARY KEY,
    work_id TEXT NOT NULL REFERENCES works(id) ON DELETE CASCADE,
    scope TEXT NOT NULL,              -- node / volume / work
    ref_id TEXT NOT NULL,
    ref_ordinal INTEGER DEFAULT 0,
    content TEXT NOT NULL,
    -- 生成这一份摘要时的内容指纹（节摘要用未 strip 的原文，与 nodes.content_hash 同口径）。
    -- 空串 = 已知过期。老库缺这一列，由 store._migrate() 补，别指望 CREATE TABLE IF NOT EXISTS。
    ref_hash TEXT DEFAULT '',
    model TEXT DEFAULT '',
    created_at REAL NOT NULL,
    UNIQUE(scope, ref_id)
);

CREATE TABLE IF NOT EXISTS index_state (
    work_id TEXT PRIMARY KEY REFERENCES works(id) ON DELETE CASCADE,
    last_chunk_ordinal INTEGER DEFAULT 0,
    last_graph_ordinal INTEGER DEFAULT 0,
    last_summary_ordinal INTEGER DEFAULT 0,
    dirty INTEGER DEFAULT 0,
    updated_at REAL NOT NULL
);
"""

# 内置模板：outline 里的 target_words 是软约束（实测偏差约 ±20%~40%）
BUILTIN_TEMPLATES: list[dict] = [
    {
        "id": "tpl_tech",
        "kind": "article",
        "subtype": "tech",
        "name": "技术方案 / 踩坑记录",
        "outline": [
            {"heading": "背景与问题", "prompt": "要解决什么问题、现状痛点是什么", "target_words": 600},
            {"heading": "方案概述", "prompt": "核心思路与选型理由，不展开细节", "target_words": 800},
            {"heading": "落地步骤", "prompt": "分步骤说明，每步含命令/配置/预期输出", "target_words": 1500},
            {"heading": "常见坑", "prompt": "3-5 个实际遇到的坑、现象与解决办法", "target_words": 1000},
            {"heading": "结论", "prompt": "适用边界，什么场景不要用这套", "target_words": 400},
        ],
        "style": "面向有经验的后端工程师；直接、不寒暄；不用「首先/其次/最后」；代码块必须带语言标注",
        "checks": ["term_inconsistent", "structure_incomplete", "citation_missing"],
    },
    {
        "id": "tpl_history",
        "kind": "article",
        "subtype": "history",
        "name": "历史专题",
        "outline": [
            {"heading": "时代背景", "prompt": "政治/经济/社会背景，交代清楚时间坐标", "target_words": 800},
            {"heading": "事件经过", "prompt": "按时间顺序叙述，关键节点不省略", "target_words": 1500},
            {"heading": "影响与后果", "prompt": "短期与长期影响，区分确定与推测", "target_words": 1000},
            {"heading": "史料辨析", "prompt": "不同记载的差异、存疑处明确标注", "target_words": 800},
            {"heading": "结语", "prompt": "历史定位，不做过度的现实类比", "target_words": 300},
        ],
        "style": "书面、克制，不用现代网络用语；对存疑处写「一说」「存疑」",
        "checks": ["citation_missing", "structure_incomplete"],
    },
    {
        "id": "tpl_politics",
        "kind": "article",
        "subtype": "politics",
        "name": "时政分析",
        "outline": [
            {"heading": "事实陈述", "prompt": "已确认的事实，标注来源与日期", "target_words": 600},
            {"heading": "多方立场", "prompt": "每种立场单独成段，标注来源", "target_words": 1200},
            {"heading": "影响推演", "prompt": "短期与长期，明确标注为分析/推测", "target_words": 1000},
            {"heading": "结论", "prompt": "回到事实层，不做价值判断", "target_words": 300},
        ],
        "style": "中性、陈述式；严格区分「事实」与「观点」；资料截止日期必须写进正文",
        "checks": ["fact_stale", "citation_missing", "structure_incomplete"],
    },
    {
        "id": "tpl_lifestyle",
        "kind": "article",
        "subtype": "lifestyle",
        "name": "生活推荐",
        "outline": [
            {"heading": "场景切入", "prompt": "什么情况下会遇到这个问题", "target_words": 400},
            {"heading": "清单", "prompt": "3-6 个具体选项，每个给适用场景", "target_words": 1200},
            {"heading": "对比", "prompt": "关键差异表，不堆参数", "target_words": 600},
            {"heading": "建议", "prompt": "怎么选、谁不适合，给明确结论", "target_words": 500},
        ],
        "style": "用第二人称、口语化表达，像朋友推荐而不是广告文案；禁止「最好/第一/唯一/100%」",
        "checks": ["absolute_claim", "structure_incomplete"],
    },
    {
        "id": "tpl_general",
        "kind": "article",
        "subtype": "general",
        "name": "通用文章",
        "outline": [
            {"heading": "引言", "prompt": "提出问题，给读者一个继续读的理由", "target_words": 400},
            {"heading": "主体", "prompt": "展开论述，分 2-3 个小节", "target_words": 1500},
            {"heading": "结论", "prompt": "收束观点，给可执行的建议", "target_words": 400},
        ],
        "style": "清楚、具体，避免空话与套话",
        "checks": ["structure_incomplete"],
    },
    {
        "id": "tpl_report",
        "kind": "article",
        "subtype": "report",
        "name": "行业报告 / 白皮书",
        "outline": [
            {"heading": "摘要", "prompt": "核心结论 3 条，每条一句话", "target_words": 400},
            {"heading": "现状", "prompt": "市场规模、格局、关键数据", "target_words": 1500},
            {"heading": "驱动因素", "prompt": "技术/政策/需求三方面", "target_words": 1200},
            {"heading": "趋势判断", "prompt": "未来 1-3 年，标注置信度", "target_words": 1200},
            {"heading": "建议", "prompt": "面向不同角色分别给建议", "target_words": 600},
        ],
        "style": "数据优先，每个判断后面跟依据；不确定的地方写明「待验证」",
        "checks": ["citation_missing", "structure_incomplete"],
    },
    {
        "id": "tpl_novel",
        "kind": "novel",
        "subtype": "",
        "name": "长篇小说",
        "outline": [
            {"heading": "全书主线", "prompt": "一句话核心冲突 + 三幕结构", "target_words": 600},
            {"heading": "分卷规划", "prompt": "每卷一句话梗概与关键转折", "target_words": 800},
        ],
        "style": "第三人称；多写动作与细节，少写心理独白；每章结尾留钩子",
        "checks": ["dead_character", "item_conflict", "time_regression", "foreshadow_overdue"],
    },
]
