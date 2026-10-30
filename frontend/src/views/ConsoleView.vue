<template>
  <div class="app-shell">
    <AppNav />
    <main id="main" class="console-main">
      <header class="console-header">
        <div class="title-block">
          <h1>控制台</h1>
          <p class="subtitle">Provider 状态、浏览器画面、请求日志与维护入口</p>
        </div>
        <div class="header-actions">
          <span v-if="reloadMsg" class="reload-msg">{{ reloadMsg }}</span>
          <span v-if="authHint" class="reload-msg is-bad">{{ authHint }}</span>
          <button class="btn btn-sm" :disabled="loading" @click="refreshAll">
            <AppIcon name="refresh" :size="14" />
            {{ loading ? '刷新中…' : '刷新' }}
          </button>
        </div>
      </header>

      <!-- tab 用 query 表达：/admin?tab=live&provider=deepseek，刷新和分享都稳定 -->
      <nav class="console-tabs" role="tablist" aria-label="控制台分区">
        <button
          v-for="t in TABS"
          :key="t.id"
          type="button"
          role="tab"
          class="console-tab"
          :class="{ 'is-on': tab === t.id }"
          :aria-selected="tab === t.id"
          @click="selectTab(t.id)"
        >
          <AppIcon :name="t.icon" :size="14" />
          {{ t.label }}
          <span v-if="t.id === 'logs' && logs.length" class="tab-count">{{ logs.length }}</span>
          <span v-if="t.id === 'live' && liveOn" class="tab-live" title="画面正在推流"></span>
        </button>
      </nav>

      <!-- ---------- 状态 ---------- -->
      <section v-if="tab === 'status'" class="tab-pane" role="tabpanel">
        <section class="kpis" aria-label="关键指标">
          <div class="kpi card">
            <span class="kpi-label"><AppIcon name="activity" :size="14" /> 请求总数</span>
            <span class="kpi-value">{{ stats.total ?? '—' }}</span>
            <span class="kpi-foot">最近 500 条环形缓冲</span>
          </div>
          <div class="kpi card">
            <span class="kpi-label"><AppIcon name="zap" :size="14" /> 平均耗时</span>
            <span class="kpi-value">{{ stats.avg_ms != null ? stats.avg_ms + ' ms' : '—' }}</span>
            <span class="kpi-foot">仅统计已完成的请求</span>
          </div>
          <div class="kpi card">
            <span class="kpi-label"><AppIcon name="alert" :size="14" /> 错误率</span>
            <span class="kpi-value" :class="{ danger: stats.error_rate > 5 }">{{ stats.error_rate ?? 0 }}%</span>
            <span class="kpi-foot">HTTP ≥ 400 占比</span>
          </div>
          <div class="kpi card">
            <span class="kpi-label"><AppIcon name="cpu" :size="14" /> 可用模型</span>
            <span class="kpi-value">{{ totalModels }}</span>
            <span class="kpi-foot">{{ providers.length }} 个 Provider</span>
          </div>
        </section>

        <section class="card">
          <div class="card-head">
            <span class="card-title">Provider 状态</span>
            <span class="card-sub">{{ readyCount }}/{{ providers.length }} 就绪</span>
            <span class="head-spacer"></span>
            <button class="btn btn-sm" @click="openLive()">打开内置浏览器</button>
          </div>
          <div class="card-body">
            <div class="provider-cards">
              <article v-for="p in providers" :key="p.provider" class="provider-card">
                <div class="pc-head">
                  <div class="pc-name-wrap">
                    <span class="pc-name">{{ p.display_name || p.provider }}</span>
                    <span class="pc-key">{{ p.provider }}</span>
                  </div>
                  <span class="pill" :class="p.has_profile ? 'pill--on' : 'pill--warn'">
                    {{ p.has_profile ? '已登录' : '待登录' }}
                  </span>
                </div>

                <div class="pc-status">
                  <span class="pc-status-item">
                    <span class="status-dot" :class="p.registered ? 'on' : 'off'"></span>
                    已注册
                  </span>
                  <span class="pc-status-item">
                    <span class="status-dot" :class="p.has_profile ? 'on' : 'off'"></span>
                    Profile
                  </span>
                  <span class="pc-status-item">
                    <span class="status-dot" :class="p.browser_open ? 'on' : 'off'"></span>
                    浏览器
                  </span>
                </div>

                <div class="pc-models">
                  <span v-for="m in modelTags(p)" :key="m.id" class="tag" :title="m.id">{{ m.label }}</span>
                  <span v-if="!modelTags(p).length" class="pc-no-model">暂无模型</span>
                </div>

                <div class="pc-caps">
                  <span class="pc-caps-key">模式</span>
                  <span v-for="mo in capsOf(p).modes" :key="mo.id" class="tag tag--mode">{{ mo.label }}</span>
                  <span v-if="!capsOf(p).modes.length" class="pc-no-model">—</span>
                </div>

                <div class="pc-align" :class="alignClass(p)">
                  <span class="pc-caps-key">最近对齐</span>
                  <span>{{ alignText(p) }}</span>
                </div>

                <button class="pc-link" type="button" @click="openLive(p.provider)">
                  打开 {{ p.provider }} 浏览器
                  <AppIcon name="chevronRight" :size="13" />
                </button>
              </article>
              <div v-if="!providers.length" class="empty-block">
                <AppIcon name="cpu" :size="18" />
                <span>暂无 Provider，检查 config.yaml</span>
              </div>
            </div>
          </div>
        </section>

        <section class="card">
          <div class="card-head">
            <span class="card-title">快捷操作</span>
            <span class="card-sub">常用维护入口</span>
          </div>
          <div class="card-body actions">
            <button class="action-row" type="button" @click="openLive()">
              <span class="action-icon"><AppIcon name="monitor" :size="16" /></span>
              <span class="action-text">
                <strong>内嵌浏览器</strong>
                <small>登录站点、录制接口、处理人机验证</small>
              </span>
              <AppIcon name="chevronRight" :size="15" class="action-arrow" />
            </button>
            <button class="action-row" type="button" @click="selectTab('tasks')">
              <span class="action-icon"><AppIcon name="layers" :size="16" /></span>
              <span class="action-text">
                <strong>任务队列</strong>
                <small>逐节生成的进度、失败重试与抽检判定</small>
              </span>
              <AppIcon name="chevronRight" :size="15" class="action-arrow" />
            </button>
            <button class="action-row" type="button" @click="selectTab('data')">
              <span class="action-icon"><AppIcon name="database" :size="16" /></span>
              <span class="action-text">
                <strong>数据与发布台账</strong>
                <small>删除备份恢复、公众号发布记录</small>
              </span>
              <AppIcon name="chevronRight" :size="15" class="action-arrow" />
            </button>
            <div class="action-row is-static">
              <span class="action-icon"><AppIcon name="key" :size="16" /></span>
              <span class="action-text">
                <strong>OpenAI 兼容端点</strong>
                <small class="mono">POST /v1/chat/completions · GET /v1/models</small>
              </span>
            </div>
          </div>
        </section>
      </section>

      <!-- ---------- 浏览器 ----------
           首进才 mount，之后切 tab 只隐藏不销毁：屏摄是 CDP 流，销毁就丢现场。
           active=false 时组件自己停流，切回来重连（见 LiveConsole 底部的 watch）。 -->
      <section v-show="tab === 'live'" class="tab-pane tab-pane--fill" role="tabpanel">
        <LiveConsole
          v-if="liveOpened"
          ref="liveRef"
          :active="tab === 'live'"
          :provider="provider"
          :auto-connect="autoConnect"
          :initial-url="String(route.query.url || '')"
          @update:provider="onProvider"
          @update:connected="onStreaming"
        />
      </section>

      <!-- ---------- 日志 ---------- -->
      <section v-if="tab === 'logs'" class="tab-pane" role="tabpanel">
        <section class="card">
          <div class="card-head">
            <span class="card-title">告警</span>
            <span class="card-sub">{{ alerts.length }} 条</span>
            <span class="head-spacer"></span>
            <span v-if="alertsError" class="log-error">{{ alertsError }}</span>
          </div>
          <div class="alert-list">
            <div v-for="a in alerts" :key="a.seq" class="alert-row" :class="'level-' + (a.level || 'warn')">
              <span class="alert-kind">{{ KIND_LABEL[a.kind] || a.kind }}</span>
              <span class="alert-provider">{{ a.provider || '—' }}</span>
              <span class="alert-text">{{ a.message }}</span>
              <span class="alert-time mono">{{ fmtTime(a.ts) }}</span>
            </div>
            <div v-if="!alerts.length" class="empty-block">
              <AppIcon name="check" :size="18" />
              <span>最近没有告警：没有验证码、风控冷却或登录失效</span>
            </div>
          </div>
        </section>

        <section class="card">
          <div class="card-head">
            <span class="card-title">最近请求</span>
            <span class="card-sub">{{ logs.length }} 条</span>
            <span class="head-spacer"></span>
            <span v-if="logsError" class="log-error">{{ logsError }}</span>
          </div>
          <div class="table-wrap">
            <table class="req-table">
              <thead>
                <tr>
                  <th>时间</th>
                  <th>方法</th>
                  <th>路径</th>
                  <th class="col-model">模型</th>
                  <th>状态</th>
                  <th class="col-ms">耗时</th>
                </tr>
              </thead>
              <tbody>
                <tr v-for="(l, i) in logs" :key="i">
                  <td class="mono">{{ fmtTime(l.ts) }}</td>
                  <td class="mono">{{ l.method }}</td>
                  <td class="mono path-cell">{{ l.path }}</td>
                  <td class="mono col-model">{{ l.model || '—' }}</td>
                  <td>
                    <span class="status-code" :class="l.status >= 400 ? 'bad' : 'good'">{{ l.status }}</span>
                  </td>
                  <td class="mono col-ms">{{ l.duration_ms ? Math.round(l.duration_ms) + ' ms' : '—' }}</td>
                </tr>
              </tbody>
            </table>
            <div v-if="!logs.length" class="empty-block">
              <AppIcon name="activity" :size="18" />
              <span>还没有 API 请求记录，调用一次 /v1/chat/completions 后出现</span>
            </div>
          </div>
        </section>
      </section>

      <!-- ---------- 任务：队列与单条 job（批次四从写作台迁入） ---------- -->
      <section v-else-if="tab === 'tasks'" class="tab-pane" role="tabpanel">
        <section class="card">
          <div class="card-head">
            <span class="card-title">任务队列</span>
            <span class="card-sub">{{ queueSub }}</span>
            <span class="head-spacer"></span>
            <span v-if="queueError" class="log-error">{{ queueError }}</span>
            <span v-if="worksError" class="log-error">{{ worksError }}</span>
            <button v-if="queue?.paused" class="btn btn-sm" @click="resumeQueue">继续</button>
            <button v-else class="btn btn-sm" :disabled="!queue" @click="pauseQueue">暂停</button>
            <button class="btn btn-sm" title="进程被杀后卡在「进行中」的任务退回队列" @click="recoverQueue">
              恢复卡住
            </button>
            <button class="btn btn-sm" :disabled="tasksBusy" @click="refreshTasks">
              {{ tasksBusy ? '刷新中…' : '刷新' }}
            </button>
          </div>
          <div class="card-body">
            <div class="q-stats">
              <span class="q-stat"><b>{{ counts.queued || 0 }}</b> 排队</span>
              <span class="q-stat"><b>{{ counts.running || 0 }}</b> 进行中</span>
              <span class="q-stat"><b>{{ counts.done || 0 }}</b> 完成</span>
              <span v-if="counts.failed" class="q-stat is-bad"><b>{{ counts.failed }}</b> 失败</span>
              <span v-if="counts.cancelled" class="q-stat"><b>{{ counts.cancelled }}</b> 已取消</span>
              <span v-if="queue?.eta_sec" class="q-eta">预计还要 {{ etaText }}</span>
            </div>
            <p v-if="queue?.current" class="q-now">
              <span class="spinner" aria-hidden="true"></span>
              正在写：{{ queue.current.title || queue.current.task_type }}
              <span class="mono q-now-task">{{ queue.current.task_type }}</span>
            </p>
            <p v-else-if="queue && !queue.worker" class="hint-line">
              worker 空闲（入队后自动拉起）；关掉写作台不影响队列继续跑。
            </p>
            <p v-if="queue?.paused" class="q-warn">已暂停：{{ queue.reason }}</p>
            <p v-else-if="queue?.last_error" class="q-warn">上次异常：{{ queue.last_error }}</p>
            <p v-if="queueNote" class="hint-line">{{ queueNote }}</p>

            <!-- 抽检卡点：连续两次「重写」整队停下，判定在控制台给 -->
            <div v-if="queue?.triage?.due" class="q-triage">
              <p class="q-triage-title">抽检：这两节能用吗？连续两次「重写」会暂停整条队列</p>
              <div class="q-triage-list">
                <span v-for="s in queue.triage.samples.slice(0, 2)" :key="s.node_id" class="q-triage-item">
                  {{ s.title || s.node_id }}
                  <b>{{ s.score ?? '—' }}</b>
                </span>
              </div>
              <div class="q-triage-act">
                <button class="btn btn-sm" @click="triageQueue('ok')">可用，继续</button>
                <button class="btn btn-sm" @click="triageQueue('rewrite')">要重写</button>
              </div>
            </div>
          </div>
        </section>

        <section class="card">
          <div class="card-head">
            <span class="card-title">任务明细</span>
            <span class="card-sub">{{ jobs.length }} 条</span>
            <span class="head-spacer"></span>
            <select v-model="jobFilter" class="field field--sm" aria-label="按状态筛选任务">
              <option value="">全部状态</option>
              <option value="queued">排队</option>
              <option value="running">进行中</option>
              <option value="done">完成</option>
              <option value="failed">失败</option>
              <option value="cancelled">已取消</option>
            </select>
            <span v-if="jobsError" class="log-error">{{ jobsError }}</span>
          </div>
          <div class="table-wrap">
            <table class="req-table">
              <thead>
                <tr>
                  <th>时间</th>
                  <th>作品</th>
                  <th>节</th>
                  <th>任务</th>
                  <th>状态</th>
                  <th class="col-ms">尝试</th>
                  <th class="col-model">最后错误</th>
                  <th>操作</th>
                </tr>
              </thead>
              <tbody>
                <tr v-for="j in filteredJobs" :key="j.id">
                  <td class="mono">{{ fmtStamp(j.updated_at) }}</td>
                  <td class="path-cell" :title="j.work_id">{{ workName(j.work_id) }}</td>
                  <td class="path-cell">{{ j.title || '—' }}</td>
                  <td class="mono">{{ j.task_type }}</td>
                  <td>
                    <span class="job-state" :class="'is-' + j.status">
                      {{ JOB_LABEL[j.status] || j.status }}
                    </span>
                  </td>
                  <td class="mono col-ms">{{ j.attempts }}/{{ j.max_attempts }}</td>
                  <td class="col-model err-cell" :title="j.last_error">{{ j.last_error || '—' }}</td>
                  <td class="act-cell">
                    <button v-if="j.status === 'failed'" class="mini-link" @click="retryJob(j)">重试</button>
                    <button
                      v-if="j.status === 'queued' || j.status === 'failed'"
                      class="mini-link"
                      @click="cancelJob(j)"
                    >取消</button>
                    <span v-if="j.status !== 'failed' && j.status !== 'queued'" class="dash">—</span>
                  </td>
                </tr>
              </tbody>
            </table>
            <div v-if="!filteredJobs.length" class="empty-block">
              <AppIcon name="layers" :size="18" />
              <span v-if="jobFilter">这个状态下一个任务都没有</span>
              <span v-else>队列是空的：在写作台点「排队」，关页面也会继续跑</span>
            </div>
          </div>
        </section>
      </section>

      <!-- ---------- 数据：发布台账 / 备份（批次四从写作台迁入） ---------- -->
      <section v-else-if="tab === 'data'" class="tab-pane" role="tabpanel">
        <section class="card">
          <div class="card-head">
            <span class="card-title">发布台账</span>
            <span class="card-sub">{{ pubSummary.length }} 篇作品有过发布记录</span>
            <span class="head-spacer"></span>
            <span v-if="worksError || dataError" class="log-error">{{ worksError || dataError }}</span>
            <button class="btn btn-sm" :disabled="dataBusy" @click="refreshData">
              {{ dataBusy ? '刷新中…' : '刷新' }}
            </button>
          </div>
          <div class="table-wrap">
            <table class="req-table">
              <thead>
                <tr>
                  <th>作品</th>
                  <th>篇数</th>
                  <th>总阅读</th>
                  <th>在看</th>
                  <th>最近发布</th>
                  <th>链接</th>
                </tr>
              </thead>
              <tbody>
                <tr v-for="p in pubSummary" :key="p.work_id">
                  <td class="path-cell">{{ p.work_title }}</td>
                  <td class="mono">{{ p.pubs }}</td>
                  <td class="mono">{{ p.reads }}</td>
                  <td class="mono">{{ p.likes }}</td>
                  <td class="mono">{{ p.last_at || '—' }}</td>
                  <td>
                    <a v-if="p.last_url" class="ext-link" :href="p.last_url" target="_blank" rel="noreferrer">打开</a>
                    <span v-else>—</span>
                  </td>
                </tr>
              </tbody>
            </table>
            <div v-if="!pubSummary.length" class="empty-block">
              <AppIcon name="send" :size="18" />
              <span>还没有发布记录：发出去之后在下面「单篇发布记录」里记一条</span>
            </div>
          </div>
        </section>

        <section class="card">
          <div class="card-head">
            <span class="card-title">单篇发布记录</span>
            <span class="card-sub">公众号没有接口，链接与阅读数只能手记</span>
            <span class="head-spacer"></span>
            <select v-model="pubWork" class="field field--sm" aria-label="选择作品" @change="loadPubs">
              <option value="">选一篇作品</option>
              <option v-for="w in articleWorks" :key="w.id" :value="w.id">{{ w.title }}</option>
            </select>
            <button class="btn btn-sm" :disabled="!pubWork || checkBusy" @click="runPublishCheck">
              {{ checkBusy ? '检查中…' : '发布体检' }}
            </button>
          </div>
          <div class="card-body">
            <template v-if="checkResult">
              <p class="chk-stats">
                {{ checkResult.stats.sections }} 节 · {{ checkResult.stats.chars }} 字 · 约
                {{ checkResult.stats.read_minutes }} 分钟读完 · 提醒 {{ checkWarn }} · 提示 {{ checkInfo }}
              </p>
              <ul class="chk-list">
                <li v-for="(it, i) in checkItems" :key="i" class="chk-item" :class="'sev-' + it.severity">
                  <span class="chk-sev">{{ it.severity === 'warn' ? '提醒' : '提示' }}</span>
                  <span class="chk-msg">{{ it.message }}</span>
                </li>
              </ul>
              <p v-if="checkMore" class="hint-line">
                还有 {{ checkMore }} 条；完整清单在写作台「公众号」弹层里，改完再复制。
              </p>
            </template>
            <p v-else class="hint-line">
              选一篇作品点「发布体检」：查长段落、首屏、摘要、配图这些公众号排版问题，纯本地比对不调模型。
            </p>

            <template v-if="pubWork">
              <div class="pub-records">
                <div v-for="p in pubs" :key="p.id" class="pub-line">
                  <span class="mono pub-line-date">{{ p.published_at || '未填日期' }}</span>
                  <span class="pub-line-num">阅读 {{ p.reads }} · 在看 {{ p.likes }}</span>
                  <a v-if="p.url" class="ext-link" :href="p.url" target="_blank" rel="noreferrer">链接</a>
                  <button class="mini-link" @click="removePub(p)">删</button>
                </div>
                <p v-if="!pubs.length" class="hint-line">这篇还没有发布记录。</p>
                <div class="pub-form">
                  <input v-model="pubDraft.published_at" class="field field--sm" placeholder="2026-10-01" aria-label="发布日期" />
                  <input v-model="pubDraft.url" class="field field--sm" placeholder="文章链接（可空）" aria-label="文章链接" />
                  <input v-model.number="pubDraft.reads" class="field field--sm" type="number" placeholder="阅读" aria-label="阅读数" />
                  <input v-model.number="pubDraft.likes" class="field field--sm" type="number" placeholder="在看" aria-label="在看数" />
                  <button class="btn btn-sm" :disabled="pubBusy" @click="savePub">记一条</button>
                </div>
              </div>
            </template>
          </div>
        </section>

        <section class="card">
          <div class="card-head">
            <span class="card-title">删除备份</span>
            <span class="card-sub">{{ nodeBackups.length }} 份节点备份 · {{ workBackups.length }} 份整库副本</span>
            <span class="head-spacer"></span>
            <span class="mono card-path">{{ backupDir }}</span>
          </div>
          <div class="table-wrap">
            <table class="req-table">
              <thead>
                <tr>
                  <th>时间</th>
                  <th>作品</th>
                  <th>节点</th>
                  <th class="col-ms">大小</th>
                  <th>操作</th>
                </tr>
              </thead>
              <tbody>
                <tr v-for="b in nodeBackups" :key="b.name">
                  <td class="mono">{{ fmtStamp(b.created_at) }}</td>
                  <td class="path-cell" :title="b.work_id">{{ workName(b.work_id) }}</td>
                  <td class="path-cell">{{ b.title || '（未命名）' }}</td>
                  <td class="mono col-ms">{{ fmtBytes(b.bytes) }}</td>
                  <td class="act-cell">
                    <button class="mini-link" :disabled="restoringBackup" @click="restoreBackup(b)">恢复</button>
                  </td>
                </tr>
              </tbody>
            </table>
            <div v-if="!nodeBackups.length" class="empty-block">
              <AppIcon name="database" :size="18" />
              <span>还没有节点备份：删节之前服务端会自动存一份，恢复会作为新节追加到末尾</span>
            </div>
          </div>
          <div class="card-body">
            <p v-if="dataNote" class="hint-line">{{ dataNote }}</p>
            <p class="hint-line">整库副本（删作品前自动落一份）只能停服务后手动盖回，文件名：</p>
            <div v-for="w in workBackups" :key="w.name" class="bak-line">
              <span class="mono bak-name">{{ w.name }}</span>
              <span class="bak-meta">{{ fmtStamp(w.created_at) }} · {{ fmtBytes(w.bytes) }}</span>
            </div>
            <p v-if="!workBackups.length" class="hint-line">还没有整库副本。</p>
          </div>
        </section>
      </section>

      <!-- ---------- 配置：重载 / 公众号设置 / 内容类型（批次四从写作台迁入） ---------- -->
      <section v-else-if="tab === 'config'" class="tab-pane" role="tabpanel">
        <section class="card">
          <div class="card-head">
            <span class="card-title">站点配置</span>
            <span class="card-sub">config.yaml 的热重载</span>
            <span class="head-spacer"></span>
            <button class="btn btn-sm" :disabled="reloading" @click="reloadConfig">
              {{ reloading ? '重载中…' : '重载配置' }}
            </button>
          </div>
          <div class="card-body">
            <p class="hint-line" :class="{ 'is-bad': reloadBad }">{{ reloadNote || '重载会就地重建适配器，不重启进程；.env 里的端口与访问口令仍需重启才生效。' }}</p>
          </div>
        </section>

        <section class="card">
          <div class="card-head">
            <span class="card-title">公众号设置</span>
            <span class="card-sub">跟着作者走，不属于某一篇</span>
            <span class="head-spacer"></span>
            <span v-if="configError" class="log-error">{{ configError }}</span>
            <span class="mono card-path">/studio/settings</span>
          </div>
          <div class="card-body">
            <div class="cfg-row">
              <span class="cfg-name">版式主题</span>
              <span class="cfg-meta">{{ settings.theme || '未设置（导出时按纸感稿件）' }}</span>
            </div>
            <div class="cfg-row">
              <span class="cfg-name">文末引导</span>
              <span class="cfg-meta cfg-meta--pre">{{ settings.footer_text || '未设置' }}</span>
            </div>
            <div class="cfg-row">
              <span class="cfg-name">文末归档页</span>
              <span class="cfg-meta">{{ settings.footer_archive === '1' ? '开' : '关' }}</span>
            </div>
            <div class="cfg-row">
              <span class="cfg-name">文风画像</span>
              <span class="cfg-meta cfg-meta--pre">{{ voicePreview }}</span>
            </div>
            <p class="hint-line">这几项在写作台右上角「公众号」弹层里改，导出 HTML 与复制富文本都按这里走。</p>
          </div>
        </section>

        <section class="card">
          <div class="card-head">
            <span class="card-title">内容类型</span>
            <span class="card-sub">{{ contentTypes.length }} 种文体（content-types.yaml）</span>
            <span class="head-spacer"></span>
            <span v-if="configError" class="log-error">{{ configError }}</span>
            <span class="mono card-path">新增文种只改 YAML，不用动前端</span>
          </div>
          <div class="card-body">
            <div v-for="k in contentTypes" :key="k.id" class="ct-kind">
              <div class="ct-head">
                <span class="ct-name">{{ k.label }}</span>
                <span class="mono ct-id">{{ k.id }}</span>
                <span class="ct-budget">上下文预算 {{ k.budget_chars }} 字</span>
              </div>
              <div class="ct-scales">
                <span v-for="s in k.scales" :key="s.id" class="ct-scale">
                  {{ s.label }} · {{ s.leaf_label }}<template v-if="s.container_label">/{{ s.container_label }}</template>
                  <template v-if="s.target_words"> · 约 {{ s.target_words }} 字</template>
                </span>
              </div>
              <div class="ct-tags">
                <span v-for="s in k.subtypes" :key="s.id" class="tag">{{ s.label }}</span>
                <span v-if="!k.subtypes.length" class="hint-line inline">没有细分文种</span>
              </div>
            </div>
            <p v-if="!contentTypes.length" class="hint-line">读不到 content-types.yaml：检查服务端配置目录。</p>
          </div>
        </section>
      </section>
    </main>
  </div>
</template>

<script setup>
import { computed, nextTick, onMounted, onUnmounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useProviderStore } from '../stores/provider'
import { apiFetch } from '../utils/api'
import AppNav from '../components/AppNav.vue'
import AppIcon from '../components/AppIcon.vue'
import LiveConsole from '../components/LiveConsole.vue'

const TABS = [
  { id: 'status', label: '状态', icon: 'activity' },
  { id: 'live', label: '浏览器', icon: 'monitor' },
  { id: 'logs', label: '日志', icon: 'terminal' },
  { id: 'tasks', label: '任务', icon: 'layers' },
  { id: 'data', label: '数据', icon: 'database' },
  { id: 'config', label: '配置', icon: 'sliders' },
]

const JOB_LABEL = {
  queued: '排队', running: '进行中', done: '完成', failed: '失败', cancelled: '已取消',
}

const KIND_LABEL = {
  captcha: '需要人机验证',
  risk: '风控冷却中',
  login: '登录态失效',
  provider: '上游异常',
}

const route = useRoute()
const router = useRouter()
const providerStore = useProviderStore()

const providers = ref([])
const stats = ref({ total: 0, avg_ms: 0, error_rate: 0, by_provider: {} })
const logs = ref([])
const logsError = ref('')
const alerts = ref([])
const alertsError = ref('')
const loading = ref(false)
const reloadMsg = ref('')
const authHint = ref('')

const tab = ref(normalizeTab(route.query.tab))
const provider = ref(route.query.provider || '')
// 只有「带着 provider 进来」或点了 Provider 卡片才自动连；单纯点浏览器 tab 不连，
// 避免一进控制台就在后台拉起浏览器
const autoConnect = ref(Boolean(route.query.provider))
const liveOpened = ref(tab.value === 'live')
const liveRef = ref(null)
const liveOn = ref(false)

// ---- 任务（批次四从写作台迁入） ----
const queue = ref(null)
const queueError = ref('')
const queueNote = ref('')
const jobs = ref([])
const jobsError = ref('')
const jobFilter = ref('')
const tasksBusy = ref(false)
// 轮询自己的“正在拉”标记：和 tasksBusy 分开，否则 3 秒一次会把「刷新」按钮一直写成刷新中
let tasksInFlight = false
let tasksTimer = 0

// ---- 数据 ----
const works = ref([])
const pubSummary = ref([])
const worksError = ref('')
const pubWork = ref('')
const pubs = ref([])
const pubDraft = ref({ published_at: '', url: '', reads: 0, likes: 0 })
const pubBusy = ref(false)
const checkResult = ref(null)
const checkBusy = ref(false)
const nodeBackups = ref([])
const workBackups = ref([])
const backupDir = ref('')
const restoringBackup = ref(false)
const dataError = ref('')
const dataNote = ref('')
const dataBusy = ref(false)

// ---- 配置 ----
const settings = ref({})
const contentTypes = ref([])
const configError = ref('')
const reloadNote = ref('')
const reloadBad = ref(false)
const reloading = ref(false)

const readyCount = computed(() => providers.value.filter(p => p.has_profile).length)
const totalModels = computed(() => providers.value.reduce((n, p) => n + (p.models?.length || 0), 0))

watch(tab, (v) => {
  if (v === 'live') liveOpened.value = true
  // 队列只在「任务」tab 里轮询：别的 tab 不看进度，没必要每 3 秒打一次接口
  stopTasksPoll()
  if (v === 'tasks') {
    enterTasks()
    startTasksPoll()
  } else if (v === 'data') refreshData()
  else if (v === 'config') loadConfig()
})
// 直接改地址栏（或浏览器前进后退）时也要跟上
watch(() => route.query.tab, (v) => {
  const next = normalizeTab(v)
  if (next !== tab.value) tab.value = next
})
watch(() => route.query.provider, (v) => {
  const next = v || ''
  if (!next || next === provider.value) return
  provider.value = next
  autoConnect.value = true
  // 已挂在控制台里（如告警条「去处理」指向另一个站点）：props 变化会被
  // LiveConsole 的「已连着就不换」拦住，所以要把 openProvider 直接下发一次。
  // 首次进入不靠这里：那时 liveOpened 还是 false，组件挂载时 autoConnect 自己会连。
  nextTick(() => {
    if (tab.value !== 'live') return
    liveRef.value?.openProvider?.(next)
  })
})

onMounted(() => {
  refreshAll()
  if (tab.value === 'tasks') startTasksPoll()
})
// 离开控制台（或关页）必须把轮询停掉，不然走了还在每 3 秒打接口
onUnmounted(() => stopTasksPoll())

function normalizeTab(v) {
  return TABS.some(t => t.id === v) ? v : 'status'
}

function selectTab(id) {
  if (tab.value === id) return
  tab.value = id
  syncQuery()
}

function syncQuery() {
  const q = { ...route.query }
  if (tab.value === 'status') delete q.tab
  else q.tab = tab.value
  if (provider.value && tab.value === 'live') q.provider = provider.value
  else delete q.provider
  router.replace({ path: '/admin', query: q })
}

function onProvider(v) {
  if (!v || v === provider.value) return
  provider.value = v
  if (tab.value === 'live') syncQuery()
}

function onStreaming(v) {
  liveOn.value = Boolean(v)
}

// 状态卡片 / 快捷操作里点「打开浏览器」：切到浏览器 tab 并直接连上这个 provider
function openLive(who) {
  if (who) provider.value = who
  autoConnect.value = true
  tab.value = 'live'
  liveOpened.value = true
  syncQuery()
  nextTick(() => {
    if (liveRef.value && liveRef.value.openProvider) liveRef.value.openProvider(who || provider.value)
  })
}

async function refreshAll() {
  loading.value = true
  try {
    await Promise.allSettled([loadStatus(), loadStats(), loadLogs(), loadAlerts(), providerStore.fetchModels()])
  } finally {
    loading.value = false
  }
  // 刷新按钮跟着当前 tab 走：在「任务」里点刷新，得把队列拉一遍
  await refreshTab()
}

async function refreshTab() {
  if (tab.value === 'tasks') {
    tasksBusy.value = true
    try {
      await enterTasks()
    } finally {
      tasksBusy.value = false
    }
  } else if (tab.value === 'data') await refreshData()
  else if (tab.value === 'config') await loadConfig()
}

async function loadStatus() {
  await providerStore.fetchStatus()
  providers.value = providerStore.status
}

async function loadStats() {
  try {
    const r = await apiFetch('/admin/stats')
    if (r.status === 401) authHint.value = '服务端要求访问口令，点左下角钥匙设置（统计/日志拿不到数据）'
    else if (r.ok) {
      authHint.value = ''
      stats.value = await r.json()
    }
  } catch (_) {
    /* 统计不可用时保留旧值 */
  }
}

async function loadLogs() {
  logsError.value = ''
  try {
    const r = await apiFetch('/admin/logs?limit=50')
    if (r.status === 401) throw new Error('需要访问口令，点左下角钥匙设置')
    if (!r.ok) throw new Error('HTTP ' + r.status)
    logs.value = await r.json()
  } catch (e) {
    logs.value = []
    logsError.value = '日志不可用：' + e.message
  }
}

async function loadAlerts() {
  alertsError.value = ''
  try {
    const r = await apiFetch('/admin/alerts?limit=20')
    if (r.status === 401) throw new Error('需要访问口令，点左下角钥匙设置')
    if (!r.ok) throw new Error('HTTP ' + r.status)
    const j = await r.json()
    // 倒序：最新一条在最上面，不用滚到底才知道刚刚发生了什么
    alerts.value = (j.items || []).slice().reverse()
  } catch (e) {
    alerts.value = []
    alertsError.value = '告警不可用：' + e.message
  }
}

async function reloadConfig() {
  reloading.value = true
  reloadBad.value = false
  reloadMsg.value = '正在重载…'
  try {
    // 重载会重建 provider 实例，只接受 POST（GET 返回 405）
    const r = await apiFetch('/admin/reload', { method: 'POST' })
    if (r.status === 401) throw new Error('需要访问口令，点左下角钥匙设置')
    if (!r.ok) throw new Error('HTTP ' + r.status)
    const j = await r.json()
    await Promise.allSettled([loadStatus(), providerStore.fetchModels()])
    const parts = [`已重载 ${(j.providers || []).length} 个 Provider`]
    if (j.added?.length) parts.push('新增 ' + j.added.join('/'))
    if (j.removed?.length) parts.push('移除 ' + j.removed.join('/'))
    const modelTotal = Object.values(j.models || {}).reduce((n, x) => n + x, 0)
    if (modelTotal) parts.push(`${modelTotal} 个模型`)
    reloadNote.value = parts.join(' · ')
    reloadMsg.value = '配置已重载'
  } catch (e) {
    reloadBad.value = true
    reloadNote.value = '重载失败：' + e.message
    // 错误只留在配置卡里：页头那个 3 秒浮层再报一遍只是同一句话出现两次
    reloadMsg.value = ''
  } finally {
    reloading.value = false
  }
  setTimeout(() => (reloadMsg.value = ''), 3000)
}

// ==================== 任务 ====================

const counts = computed(() => queue.value?.counts || {})
const queueSub = computed(() => {
  const q = queue.value
  if (!q) return '读取中…'
  if (q.paused) return '已暂停'
  if (q.current) return '正在写'
  return q.worker ? '空闲' : 'worker 未启动'
})
const etaText = computed(() => {
  const sec = queue.value?.eta_sec || 0
  if (!sec) return ''
  return sec < 60 ? `${sec} 秒` : `${Math.ceil(sec / 60)} 分钟`
})
// 后端按「队列顺序」给前 N 条；界面上要的是「刚刚发生了什么」，所以拿 newest 再自己排一道
const filteredJobs = computed(() => {
  const rows = [...jobs.value].sort((a, b) => (b.updated_at || 0) - (a.updated_at || 0))
  const f = jobFilter.value
  return f ? rows.filter(j => j.status === f) : rows
})

function startTasksPoll() {
  if (tasksTimer) return
  tasksTimer = setInterval(() => {
    // 页面在后台时不打接口：队列自己是后台跑的，回来再拉一次就行
    if (tab.value === 'tasks' && !document.hidden) refreshTasks()
  }, 3000)
}

function stopTasksPoll() {
  if (tasksTimer) clearInterval(tasksTimer)
  tasksTimer = 0
}

async function enterTasks() {
  // 作品名只在进 tab / 手动刷新时拉：轮询里拉，接口一坏就变成每 3 秒重试一次
  if (!works.value.length) await loadWorks()
  await refreshTasks()
}

async function refreshTasks() {
  // 接口慢于 3 秒时别叠请求（手动刷新也会被这一层挡掉，下一次轮询自己就合上了）
  if (tasksInFlight) return
  tasksInFlight = true
  try {
    await Promise.all([loadQueueState(), loadJobs()])
  } finally {
    tasksInFlight = false
  }
}

async function loadQueueState() {
  try {
    const r = await apiFetch('/studio/queue/status')
    if (!r.ok) throw new Error('HTTP ' + r.status)
    queue.value = await r.json()
    queueError.value = ''
  } catch (e) {
    queueError.value = '队列不可用：' + e.message
  }
}

async function loadJobs() {
  try {
    const r = await apiFetch('/studio/jobs?limit=100&newest=true')
    if (!r.ok) throw new Error('HTTP ' + r.status)
    jobs.value = await r.json()
    jobsError.value = ''
  } catch (e) {
    jobs.value = []
    jobsError.value = '任务不可用：' + e.message
  }
}

async function pauseQueue() {
  const r = await apiFetch('/studio/queue/pause?reason=' + encodeURIComponent('控制台手动暂停'), { method: 'POST' })
  if (r.ok) queue.value = await r.json()
  else queueError.value = await errLine(r, '暂停失败')
}

async function resumeQueue() {
  const r = await apiFetch('/studio/queue/resume', { method: 'POST' })
  if (r.ok) queue.value = await r.json()
  else queueError.value = await errLine(r, '继续失败')
  // 恢复之后 worker 要重新拉起来，顺手把列表也刷一道
  await loadJobs()
}

async function recoverQueue() {
  if (!window.confirm('把卡在「进行中」的任务退回队列？进程刚被强杀时用得上。')) return
  const r = await apiFetch('/studio/queue/recover', { method: 'POST' })
  if (!r.ok) {
    queueError.value = await errLine(r, '恢复失败')
    return
  }
  const j = await r.json()
  queue.value = j.queue
  queueNote.value = `已把 ${j.recovered} 条卡住的任务退回队列`
  queueError.value = ''
  await loadJobs()
}

async function triageQueue(verdict) {
  const r = await apiFetch('/studio/queue/triage', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ verdict }),
  })
  if (r.ok) queue.value = await r.json()
  else queueError.value = await errLine(r, '抽检判定失败')
}

async function retryJob(j) {
  const r = await apiFetch(`/studio/jobs/${j.id}/retry`, { method: 'POST' })
  if (!r.ok) jobsError.value = await errLine(r, '重试失败')
  else await refreshTasks()
}

async function cancelJob(j) {
  if (!window.confirm(`取消「${j.title || j.task_type}」这条任务？`)) return
  const r = await apiFetch(`/studio/jobs/${j.id}/cancel`, { method: 'POST' })
  if (!r.ok) jobsError.value = await errLine(r, '取消失败')
  else await refreshTasks()
}

// ==================== 数据 ====================

const articleWorks = computed(() =>
  works.value.filter(w => w.kind !== 'novel' && w.status !== 'archived'),
)
const checkWarn = computed(() => (checkResult.value?.items || []).filter(i => i.severity === 'warn').length)
const checkInfo = computed(() => (checkResult.value?.items || []).filter(i => i.severity !== 'warn').length)
const checkItems = computed(() => (checkResult.value?.items || []).slice(0, 6))
const checkMore = computed(() => Math.max(0, (checkResult.value?.items || []).length - 6))

function workName(id) {
  if (!id) return '—'
  return works.value.find(w => w.id === id)?.title || id
}

async function refreshData() {
  dataBusy.value = true
  dataError.value = ''
  try {
    await Promise.all([loadWorks(), loadPubSummary(), loadBackups()])
    // 选着的作品被删了：别把一个过期 id 带到下面的接口里去
    if (pubWork.value && !works.value.some(w => w.id === pubWork.value)) {
      pubWork.value = ''
      pubs.value = []
      checkResult.value = null
    }
    if (pubWork.value) await loadPubs()
  } finally {
    dataBusy.value = false
  }
}

async function loadWorks() {
  try {
    const r = await apiFetch('/studio/works')
    if (!r.ok) throw new Error('HTTP ' + r.status)
    works.value = await r.json()
    worksError.value = ''
  } catch (e) {
    // 单独一个变量：这个请求「任务」tab 也用，沿用 dataError 的话任务页看不见
    worksError.value = '作品列表不可用：' + e.message
    dataError.value = worksError.value
  }
}

async function loadPubSummary() {
  try {
    const r = await apiFetch('/studio/publications/summary')
    if (!r.ok) throw new Error('HTTP ' + r.status)
    pubSummary.value = await r.json()
  } catch (e) {
    dataError.value = '发布台账不可用：' + e.message
  }
}

async function loadBackups() {
  try {
    const r = await apiFetch('/studio/backups')
    if (!r.ok) throw new Error('HTTP ' + r.status)
    const j = await r.json()
    nodeBackups.value = j.nodes || []
    workBackups.value = j.works || []
    backupDir.value = j.dir || ''
  } catch (e) {
    dataError.value = '备份列表不可用：' + e.message
  }
}

async function loadPubs() {
  if (!pubWork.value) return
  const r = await apiFetch(`/studio/works/${pubWork.value}/publications`)
  if (r.ok) pubs.value = await r.json()
  else dataError.value = await errLine(r, '发布记录读取失败')
}

async function savePub() {
  if (!pubWork.value) return
  pubBusy.value = true
  try {
    const r = await apiFetch(`/studio/works/${pubWork.value}/publications`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        published_at: pubDraft.value.published_at.trim(),
        url: pubDraft.value.url.trim(),
        reads: Number(pubDraft.value.reads) || 0,
        likes: Number(pubDraft.value.likes) || 0,
      }),
    })
    if (!r.ok) {
      dataError.value = await errLine(r, '保存发布记录失败')
      return
    }
    pubDraft.value = { published_at: '', url: '', reads: 0, likes: 0 }
    dataError.value = ''
    await Promise.all([loadPubs(), loadPubSummary()])
  } finally {
    pubBusy.value = false
  }
}

async function removePub(p) {
  if (!window.confirm(`删掉「${p.published_at || '未填日期'}」这条发布记录？`)) return
  const r = await apiFetch(`/studio/publications/${p.id}`, { method: 'DELETE' })
  if (!r.ok) {
    dataError.value = await errLine(r, '删除失败')
    return
  }
  await Promise.all([loadPubs(), loadPubSummary()])
}

async function runPublishCheck() {
  if (!pubWork.value) return
  checkBusy.value = true
  dataError.value = ''
  try {
    const r = await apiFetch(`/studio/works/${pubWork.value}/publish-check`)
    if (!r.ok) throw new Error('HTTP ' + r.status)
    checkResult.value = await r.json()
  } catch (e) {
    checkResult.value = null
    dataError.value = '发布体检失败：' + e.message
  } finally {
    checkBusy.value = false
  }
}

async function restoreBackup(b) {
  const where = workName(b.work_id)
  if (!window.confirm(
    `从备份找回「${b.title || '未命名'}」？\n\n`
    + `会作为新节追加到《${where}》末尾（原位置不恢复，需要的话再用上下移挪回去）；`
    + '历史版本会一起带回来。')) return
  restoringBackup.value = true
  try {
    const r = await apiFetch(`/studio/backups/${encodeURIComponent(b.name)}/restore`, { method: 'POST' })
    if (!r.ok) {
      dataError.value = await errLine(r, '恢复失败')
      return
    }
    const j = await r.json()
    dataError.value = ''
    dataNote.value = `已找回 ${j.restored?.length || 0} 节：${(j.restored || []).map(x => x.title).join('、')}`
  } finally {
    restoringBackup.value = false
  }
}

// ==================== 配置 ====================

const voicePreview = computed(() => {
  const text = (settings.value.voice_profile || '').trim()
  if (!text) return '未设置（文风样本攒几条后自动生成）'
  const head = text.replace(/\s+/g, ' ').slice(0, 60)
  return `${text.length} 字：${head}${text.length > 60 ? '…' : ''}`
})

async function loadConfig() {
  configError.value = ''
  await Promise.all([loadSettings(), loadContentTypes()])
}

async function loadSettings() {
  const r = await apiFetch('/studio/settings')
  if (r.ok) {
    settings.value = await r.json()
    return
  }
  // 读不到就把原因说出来：否则四行全显示「未设置」，看着像真的一无所有
  configError.value = await errLine(r, '设置读取失败')
}

async function loadContentTypes() {
  const r = await apiFetch('/studio/content-types')
  if (!r.ok) {
    configError.value = await errLine(r, '内容类型读取失败')
    return
  }
  const j = await r.json()
  contentTypes.value = j.kinds || []
}

async function errLine(r, label) {
  let detail = ''
  try {
    const j = await r.json()
    detail = j.detail || ''
  } catch (_) {
    /* 非 JSON 错误体，退到状态码 */
  }
  return `${label}：${detail || 'HTTP ' + r.status}`
}

function fmtStamp(ts) {
  if (!ts) return '—'
  const d = new Date(ts * 1000)
  const p = (n) => String(n).padStart(2, '0')
  return `${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`
}

function fmtBytes(n) {
  const v = Number(n) || 0
  if (v >= 1024 * 1024) return (v / 1024 / 1024).toFixed(1) + ' MB'
  return Math.max(1, Math.round(v / 1024)) + ' KB'
}

function fmtTime(ts) {
  if (!ts) return '—'
  return new Date(ts * 1000).toLocaleTimeString('zh-CN', { hour12: false })
}

// ---- 能力行：模型文案 / 模式 / 最近一次页面模式对齐结果 ----
function capsOf(p) {
  return providerStore.providers.find(c => c.key === p.provider) || { models: [], modes: [], features: [] }
}

function modelTags(p) {
  const caps = capsOf(p)
  if (caps.models && caps.models.length) return caps.models
  return (p.models || []).map(id => ({ id, label: id }))
}

function alignText(p) {
  const st = capsOf(p).last_mode_status
  if (!st) return '暂无（还没发过带模式的请求）'
  // 绑错时后端会直接跳过不点（对齐仍算 ok），但配置得修，不然模型开关形同虚设
  const warn = (st.misbind || []).length ? `｜疑似绑错：${st.misbind.join('/')}（去浏览器 tab 的高级里改回未绑定）` : ''
  if (st.ok) return `已对齐 ${st.preset || ''} ${st.ms != null ? st.ms + 'ms' : ''}${warn}`.trim()
  const miss = (st.missing || []).join('/') || '未知'
  return `未对齐：${miss}（去浏览器 tab 探测文案）${warn}`
}

function alignClass(p) {
  const st = capsOf(p).last_mode_status
  if (!st) return ''
  return st.ok ? 'is-ok' : 'is-bad'
}
</script>

<style scoped>
.app-shell {
  display: flex;
  height: 100vh;
  height: 100dvh;
  /* 兜住 dvh：它算错时整块只剩上半截。100% 走 main.css 的 html/body/#app 高链 */
  height: 100%;
  overflow: hidden;
  background: radial-gradient(1000px 500px at 80% -10%, rgba(var(--run-rgb), 0.05), transparent 60%), var(--bg);
}
.console-main {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
  overflow: hidden;
}

/* 头部 */
.console-header {
  display: flex;
  align-items: flex-end;
  justify-content: space-between;
  gap: 16px;
  flex-wrap: wrap;
  padding: 18px 26px 12px;
  flex-shrink: 0;
}
.title-block h1 {
  margin: 0 0 2px;
  font-size: 20px;
  font-weight: 600;
  letter-spacing: -0.02em;
}
.subtitle {
  margin: 0;
  font-size: 12.5px;
  color: var(--text-faint);
}
.header-actions {
  display: flex;
  align-items: center;
  gap: 10px;
}
.reload-msg {
  font-size: 12px;
  color: var(--run-fg);
}
.reload-msg.is-bad {
  color: var(--danger-fg);
}

/* tab 条 */
.console-tabs {
  display: flex;
  gap: 4px;
  padding: 0 26px;
  border-bottom: 1px solid var(--line-soft);
  flex-shrink: 0;
  overflow-x: auto;
}
.console-tab {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  height: 34px;
  padding: 0 12px;
  border: none;
  border-bottom: 2px solid transparent;
  background: none;
  color: var(--text-faint);
  font-size: 13px;
  white-space: nowrap;
  cursor: pointer;
  transition: color 0.16s var(--ease), border-color 0.16s var(--ease);
}
.console-tab:hover {
  color: var(--text);
}
.console-tab.is-on {
  color: var(--run-fg);
  border-bottom-color: var(--run);
}
.tab-count {
  font-family: theme('fontFamily.mono');
  font-size: 10.5px;
  padding: 0 5px;
  border-radius: 999px;
  background: var(--panel-3);
  color: var(--text-muted);
}
/* 画面在推流时给个点，切到别的 tab 也知道浏览器还开着 */
.tab-live {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: var(--run);
  box-shadow: 0 0 0 2px rgba(var(--run-rgb), 0.18);
}

/* tab 内容区 */
.tab-pane {
  flex: 1;
  min-height: 0;
  overflow-y: auto;
  padding: 16px 26px 28px;
  display: flex;
  flex-direction: column;
  gap: 14px;
}
.tab-pane--fill {
  padding: 0;
  overflow: hidden;
}

/* KPI */
.kpis {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 12px;
}
.kpi {
  display: flex;
  flex-direction: column;
  gap: 4px;
  padding: 14px 16px;
}
.kpi-label {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  font-size: 11.5px;
  color: var(--text-faint);
}
.kpi-value {
  font-family: theme('fontFamily.mono');
  font-size: 24px;
  font-weight: 600;
  letter-spacing: -0.02em;
  color: var(--text);
}
.kpi-value.danger {
  color: var(--danger-fg);
}
.kpi-foot {
  font-size: 11px;
  color: var(--text-faint);
}

.head-spacer {
  flex: 1;
}
.card-body {
  padding: 16px;
}
.provider-cards {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(280px, 1fr));
  gap: 12px;
}
.provider-card {
  display: flex;
  flex-direction: column;
  gap: 10px;
  padding: 14px;
  border-radius: 12px;
  border: 1px solid var(--line-soft);
  /* 卡内卡：比外层 .card（--panel）深一档，不然只剩描边能看出边界 */
  background: var(--panel-2);
  transition: border-color 0.18s var(--ease);
}
.provider-card:hover {
  border-color: var(--line);
}
.pc-head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 10px;
}
.pc-name-wrap {
  display: flex;
  flex-direction: column;
}
.pc-name {
  font-size: 13.5px;
  font-weight: 600;
}
.pc-key {
  font-family: theme('fontFamily.mono');
  font-size: 11px;
  color: var(--text-faint);
}
.pc-status {
  display: flex;
  flex-wrap: wrap;
  gap: 12px;
}
.pc-status-item {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  font-size: 11.5px;
  color: var(--text-muted);
}
.pc-models {
  display: flex;
  flex-wrap: wrap;
  gap: 5px;
}
.pc-no-model {
  font-size: 11px;
  color: var(--text-faint);
}
.pc-caps {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 5px;
}
.pc-caps-key {
  font-size: 11px;
  color: var(--text-faint);
  margin-right: 2px;
}
.tag--mode {
  color: var(--text-muted);
  border-style: dashed;
}
.pc-align {
  display: flex;
  align-items: baseline;
  gap: 6px;
  font-size: 11px;
  color: var(--text-faint);
  min-height: 16px;
}
.pc-align.is-ok {
  color: var(--run-fg);
}
.pc-align.is-bad {
  color: var(--danger-fg);
}
.pc-link {
  display: inline-flex;
  align-items: center;
  gap: 2px;
  margin-top: auto;
  padding: 2px 0 0;
  border: none;
  background: none;
  font-size: 11.5px;
  color: var(--text-faint);
  text-align: left;
  cursor: pointer;
  transition: color 0.16s var(--ease);
}
.pc-link:hover {
  color: var(--run-fg);
}

/* 快捷操作 */
.actions {
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.action-row {
  display: flex;
  align-items: center;
  gap: 12px;
  width: 100%;
  padding: 11px 12px;
  border-radius: 11px;
  border: 1px solid var(--line-soft);
  background: var(--panel-2);
  text-align: left;
  text-decoration: none;
  color: var(--text);
  cursor: pointer;
  transition: border-color 0.18s var(--ease), background-color 0.18s var(--ease);
}
.action-row:hover {
  border-color: var(--line-strong);
  background: var(--panel-3);
}
.action-row.is-static {
  cursor: default;
}
.action-row.is-static:hover {
  border-color: var(--line-soft);
  background: var(--panel-2);
}
.action-icon {
  display: flex;
  align-items: center;
  justify-content: center;
  width: 30px;
  height: 30px;
  border-radius: 9px;
  border: 1px solid var(--line-soft);
  background: var(--panel-3);
  color: var(--text-muted);
  flex-shrink: 0;
}
.action-text {
  display: flex;
  flex-direction: column;
  min-width: 0;
}
.action-text strong {
  font-size: 13px;
  font-weight: 600;
}
.action-text small {
  font-size: 11.5px;
  color: var(--text-faint);
}
.action-text small.mono {
  font-family: theme('fontFamily.mono');
}
.action-arrow {
  margin-left: auto;
  color: var(--text-faint);
}

/* 告警列表 */
.alert-list {
  display: flex;
  flex-direction: column;
}
.alert-row {
  display: grid;
  grid-template-columns: 96px 76px minmax(0, 1fr) 64px;
  align-items: baseline;
  gap: 10px;
  padding: 8px 16px;
  border-bottom: 1px solid var(--line-soft);
  font-size: 12px;
}
.alert-row:last-child {
  border-bottom: none;
}
.alert-row.level-off {
  display: none;
}
.alert-kind {
  color: var(--spark-fg);
  font-weight: 600;
}
.alert-row.level-error .alert-kind {
  color: var(--danger-fg);
}
.alert-provider {
  font-family: theme('fontFamily.mono');
  font-size: 11px;
  color: var(--text-faint);
}
.alert-text {
  color: var(--text-muted);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.alert-time {
  color: var(--text-faint);
  font-size: 11px;
  text-align: right;
}

/* 请求表 */
.table-wrap {
  overflow-x: auto;
}
.req-table {
  width: 100%;
  border-collapse: collapse;
  font-size: 12px;
}
.req-table th {
  padding: 8px 14px;
  text-align: left;
  font-weight: 500;
  font-size: 11px;
  letter-spacing: 0.04em;
  text-transform: uppercase;
  color: var(--text-faint);
  border-bottom: 1px solid var(--line-soft);
  background: var(--panel-2);
}
.req-table td {
  padding: 8px 14px;
  border-bottom: 1px solid var(--line-soft);
  color: var(--text-muted);
  white-space: nowrap;
}
.req-table tr:last-child td {
  border-bottom: none;
}
.req-table tbody tr:hover {
  background: var(--hover);
}
.mono {
  font-family: theme('fontFamily.mono');
  font-size: 11.5px;
}
.path-cell {
  max-width: 320px;
  overflow: hidden;
  text-overflow: ellipsis;
}
.status-code {
  display: inline-flex;
  align-items: center;
  height: 20px;
  padding: 0 7px;
  border-radius: 6px;
  font-family: theme('fontFamily.mono');
  font-size: 11px;
  border: 1px solid;
}
.status-code.good {
  color: var(--run-fg);
  border-color: rgba(var(--run-rgb), 0.3);
  background: rgba(var(--run-rgb), 0.08);
}
.status-code.bad {
  color: var(--danger-fg);
  border-color: rgba(var(--alert-rgb), 0.3);
  background: rgba(var(--alert-rgb), 0.08);
}
.empty-block {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 22px 16px;
  color: var(--text-faint);
  font-size: 12px;
}
.log-error {
  font-size: 11.5px;
  color: var(--danger-fg);
}

/* 空列表 / 说明行 */
.hint-line {
  margin: 0;
  font-size: 12px;
  color: var(--text-faint);
  line-height: 1.6;
}
.hint-line.is-bad {
  color: var(--danger-fg);
}
.hint-line.inline {
  display: inline;
}
.card-path {
  font-size: 11px;
  color: var(--text-faint);
}

/* 卡头里的小控件：下拉/输入统一矮一档，不和标题抢高度 */
.field--sm {
  height: 28px;
  padding: 0 8px;
  font-size: 12px;
  width: auto;
  max-width: 220px;
}

/* ---------- 任务 ---------- */
.q-stats {
  display: flex;
  flex-wrap: wrap;
  align-items: baseline;
  gap: 14px;
  font-size: 12px;
  color: var(--text-faint);
}
.q-stat b {
  margin-right: 3px;
  font-family: theme('fontFamily.mono');
  font-size: 15px;
  font-weight: 600;
  color: var(--text);
}
.q-stat.is-bad,
.q-stat.is-bad b {
  color: var(--danger-fg);
}
.q-eta {
  margin-left: auto;
  font-size: 11.5px;
  color: var(--text-faint);
}
.q-now {
  display: flex;
  align-items: center;
  gap: 8px;
  margin: 12px 0 0;
  font-size: 12.5px;
  color: var(--text-muted);
}
.q-now-task {
  font-size: 11px;
  color: var(--text-faint);
}
.q-warn {
  margin: 10px 0 0;
  font-size: 12px;
  color: var(--warn-fg);
}

/* 抽检卡点 */
.q-triage {
  margin-top: 12px;
  padding: 12px 14px;
  border-radius: 10px;
  border: 1px solid rgba(var(--run-rgb), 0.3);
  background: var(--panel-2);
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.q-triage-title {
  margin: 0;
  font-size: 12.5px;
  color: var(--text-muted);
}
.q-triage-list {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
}
.q-triage-item {
  display: inline-flex;
  align-items: baseline;
  gap: 6px;
  padding: 3px 8px;
  border-radius: 7px;
  background: var(--panel-3);
  font-size: 12px;
  color: var(--text-muted);
}
.q-triage-item b {
  font-family: theme('fontFamily.mono');
  font-weight: 600;
  color: var(--text);
}
.q-triage-act {
  display: flex;
  gap: 8px;
}

/* 任务状态墩 */
.job-state {
  display: inline-flex;
  align-items: center;
  height: 20px;
  padding: 0 7px;
  border-radius: 6px;
  border: 1px solid var(--line-soft);
  font-family: theme('fontFamily.mono');
  font-size: 11px;
  color: var(--text-faint);
}
.job-state.is-running {
  color: var(--run-fg);
  border-color: rgba(var(--run-rgb), 0.3);
  background: rgba(var(--run-rgb), 0.08);
}
.job-state.is-done {
  color: var(--text-muted);
}
.job-state.is-failed {
  color: var(--danger-fg);
  border-color: rgba(var(--alert-rgb), 0.3);
  background: rgba(var(--alert-rgb), 0.08);
}
.err-cell {
  max-width: 260px;
  overflow: hidden;
  text-overflow: ellipsis;
  color: var(--danger-fg);
}
.act-cell {
  white-space: nowrap;
}
.mini-link {
  padding: 0 4px;
  border: none;
  background: none;
  font-size: 12px;
  color: var(--run-fg);
  text-decoration: underline;
  text-underline-offset: 2px;
  cursor: pointer;
}
.mini-link:hover {
  color: var(--text);
}
.mini-link:disabled {
  color: var(--text-faint);
  cursor: default;
}
.dash {
  color: var(--text-faint);
}

/* ---------- 数据 ---------- */
.ext-link {
  color: var(--run-fg);
  text-decoration: underline;
  text-underline-offset: 2px;
}
.chk-stats {
  margin: 0 0 8px;
  font-family: theme('fontFamily.mono');
  font-size: 11.5px;
  color: var(--text-faint);
}
.chk-list {
  margin: 0;
  padding: 0;
  list-style: none;
  display: flex;
  flex-direction: column;
  gap: 6px;
}
.chk-item {
  display: flex;
  align-items: baseline;
  gap: 8px;
  font-size: 12px;
  color: var(--text-muted);
}
.chk-sev {
  flex-shrink: 0;
  font-size: 11px;
  color: var(--text-faint);
}
.chk-item.sev-warn .chk-sev {
  color: var(--warn-fg);
}
.pub-records {
  margin-top: 14px;
  padding-top: 12px;
  border-top: 1px solid var(--line-soft);
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.pub-line {
  display: flex;
  align-items: baseline;
  flex-wrap: wrap;
  gap: 12px;
  font-size: 12px;
  color: var(--text-muted);
}
.pub-line-date {
  min-width: 92px;
  color: var(--text-faint);
}
.pub-form {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  align-items: center;
}
.pub-form .field--sm {
  width: 132px;
}
.bak-line {
  display: flex;
  align-items: baseline;
  flex-wrap: wrap;
  gap: 10px;
  padding: 6px 0;
  border-bottom: 1px solid var(--line-soft);
  font-size: 12px;
}
.bak-line:last-child {
  border-bottom: none;
}
.bak-name {
  color: var(--text-muted);
  word-break: break-all;
}
.bak-meta {
  margin-left: auto;
  color: var(--text-faint);
}

/* ---------- 配置 ---------- */
.cfg-row {
  display: flex;
  align-items: baseline;
  gap: 12px;
  padding: 8px 0;
  border-bottom: 1px solid var(--line-soft);
  font-size: 12.5px;
}
.cfg-row:last-of-type {
  border-bottom: none;
}
.cfg-name {
  flex-shrink: 0;
  width: 88px;
  color: var(--text-faint);
}
.cfg-meta {
  min-width: 0;
  color: var(--text-muted);
}
.cfg-meta--pre {
  white-space: pre-wrap;
  word-break: break-word;
}
.ct-kind {
  padding: 12px 0;
  border-bottom: 1px solid var(--line-soft);
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.ct-kind:first-child {
  padding-top: 0;
}
.ct-kind:last-child {
  border-bottom: none;
  padding-bottom: 0;
}
.ct-head {
  display: flex;
  align-items: baseline;
  flex-wrap: wrap;
  gap: 10px;
}
.ct-name {
  font-size: 13.5px;
  font-weight: 600;
}
.ct-id {
  font-size: 11px;
  color: var(--text-faint);
}
.ct-budget {
  margin-left: auto;
  font-size: 11.5px;
  color: var(--text-faint);
}
.ct-scales {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
}
.ct-scale {
  padding: 3px 8px;
  border-radius: 7px;
  border: 1px solid var(--line-soft);
  background: var(--panel-2);
  font-size: 11.5px;
  color: var(--text-muted);
}
.ct-tags {
  display: flex;
  flex-wrap: wrap;
  gap: 5px;
}

@media (max-width: 1100px) {
  .kpis {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }
}
@media (max-width: 720px) {
  .console-header {
    padding: 14px 12px 10px;
  }
  .console-tabs {
    padding: 0 12px;
  }
  .tab-pane {
    padding: 12px 12px 20px;
  }
  .kpis {
    gap: 8px;
  }
  .kpi {
    padding: 12px;
  }
  .kpi-value {
    font-size: 20px;
  }
  .col-model {
    display: none;
  }
  .card-body {
    padding: 12px;
  }
  .col-ms {
    display: none;
  }
  .alert-row {
    grid-template-columns: 84px minmax(0, 1fr) 56px;
  }
  .alert-provider {
    display: none;
  }
}
</style>
