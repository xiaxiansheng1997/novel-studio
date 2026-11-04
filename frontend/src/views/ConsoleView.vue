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
            <button class="btn btn-sm" @click="openCompatDialog(null)">新增通道</button>
            <button class="btn btn-sm" @click="openLive()">打开内置浏览器</button>
          </div>
          <div class="card-body">
            <div class="provider-cards">
              <article v-for="p in providers" :key="p.provider" class="provider-card">
                <!-- 直连通道：没有登录态/profile/浏览器可谈，卡片只报
                     地址、key 状态、模型与就绪——拿反代那三个点画它全是误导 -->
                <template v-if="p.compat">
                  <div class="pc-head">
                    <div class="pc-name-wrap">
                      <span class="pc-name">{{ p.display_name || p.provider }}</span>
                      <span class="pc-key">{{ p.provider }}</span>
                    </div>
                    <span class="pill pill--info">直连通道</span>
                    <span class="pill" :class="p.enabled === false ? 'pill--off' : (p.ready ? 'pill--on' : 'pill--warn')">
                      {{ p.enabled === false ? '已停用' : (p.ready ? '就绪' : '未就绪') }}
                    </span>
                  </div>
                  <p class="pc-base mono" :title="p.base_url || ''">{{ p.base_url || '没填 base_url' }}</p>
                  <p class="pc-keyline" :class="{ 'is-bad': p.key_source === 'none' }">{{ keyLine(p) }}</p>
                  <div class="pc-models">
                    <span v-for="m in (p.models || [])" :key="m" class="tag mono" :title="m">{{ m }}</span>
                    <span v-if="!(p.models || []).length" class="pc-no-model">暂无模型</span>
                  </div>
                  <div class="pc-actions">
                    <button class="pc-link" type="button" @click="openCompatDialog(p)">配置</button>
                    <button class="pc-link" type="button" :disabled="testingProvider === p.provider" @click="testCompat(p)">
                      {{ testingProvider === p.provider ? '测试中…' : '测试' }}
                    </button>
                    <span v-if="compatTestText[p.provider]" class="pc-test-note" :class="{ 'is-bad': compatTestBad[p.provider] }">
                      {{ compatTestText[p.provider] }}
                    </span>
                  </div>
                </template>
                <template v-else>
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
                </template>
              </article>
              <div v-if="!providers.length" class="empty-block">
                <AppIcon name="cpu" :size="18" />
                <span>暂无 Provider，检查 config.yaml</span>
              </div>
            </div>
          </div>
        </section>

        <CompatDialog
          v-model:open="compatOpen"
          :entry="compatEntry"
          @saved="onCompatSaved"
        />

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
                <small>生成成本与质量、删除备份恢复、公众号发布记录</small>
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
        <!-- 成本与质量：runs 现算，不另记一份账 -->
        <section class="card">
          <div class="card-head">
            <span class="card-title">生成成本与质量</span>
            <span class="card-sub">runs 现算：按作品与按节汇总耗时、输出与评分</span>
            <span class="head-spacer"></span>
            <span class="card-sub">
              {{ cqOverall.last_at ? '最近 ' + fmtStamp(cqOverall.last_at) : '还没有生成记录' }}
            </span>
          </div>
          <div class="card-body">
            <div class="q-stats">
              <span class="q-stat"><b>{{ cqOverall.runs || 0 }}</b> 次调用</span>
              <span class="q-stat" :class="{ 'is-bad': cqOverall.failed }">
                <b>{{ cqOverall.failed || 0 }}</b> 失败
              </span>
              <span v-if="cqOverall.running" class="q-stat"><b>{{ cqOverall.running }}</b> 进行中</span>
              <span class="q-stat"><b>{{ fmtDur(cqOverall.elapsed_ms) }}</b> 总耗时</span>
              <span class="q-stat"><b>{{ fmtDur(cqOverall.avg_elapsed_ms) }}</b> 每次</span>
              <span class="q-stat"><b>{{ fmtChars(cqOverall.output_chars) }}</b> 输出</span>
              <span class="q-stat">
                <b>{{ fmtScore(cqOverall.avg_score) }}</b> 均分<template v-if="cqOverall.scored">（{{ cqOverall.scored }} 次评分）</template>
              </span>
            </div>
            <!-- 任务分解只在多于一种时才有信息量；只有一种就是上面那一行 -->
            <div v-if="cqByTask.length > 1" class="q-triage-list cq-tasks">
              <span v-for="t in cqByTask" :key="t.task_type" class="q-triage-item">
                {{ t.task_type }} <b>{{ t.runs }}</b> 次 · {{ fmtDur(t.elapsed_ms) }}
                <template v-if="t.avg_score != null">· 均分 {{ fmtScore(t.avg_score) }}</template>
              </span>
            </div>
            <p v-if="!cqWorks.length" class="hint-line">
              还没有生成记录：跑过一章或一版改写之后，这里会出现成本账。
            </p>
            <p v-else class="hint-line">
              总耗时是各次跑批 elapsed_ms 之和；「每次」只除已结束的跑批。均分只算打过分的，
              没打分不计入，也不按 0 分算；「硬告警」只数未处理的 error 级。
            </p>
          </div>
          <div v-if="cqWorks.length" class="table-wrap">
            <table class="req-table">
              <thead>
                <tr>
                  <th>作品</th>
                  <th class="col-ms">调用</th>
                  <th class="col-ms">失败</th>
                  <th class="col-ms">总耗时</th>
                  <th class="col-ms">每次</th>
                  <th class="col-ms">输出</th>
                  <th class="col-ms">均分</th>
                  <th class="col-ms">硬告警</th>
                </tr>
              </thead>
              <tbody>
                <tr v-for="w in cqWorks" :key="w.work_id">
                  <td class="path-cell" :title="w.work_id">{{ w.work_title || w.work_id }}</td>
                  <td class="mono col-ms">{{ w.runs }}</td>
                  <td class="mono col-ms" :class="{ 'cq-bad': w.failed }">{{ w.failed }}</td>
                  <td class="mono col-ms">{{ fmtDur(w.elapsed_ms) }}</td>
                  <td class="mono col-ms">{{ fmtDur(w.avg_elapsed_ms) }}</td>
                  <td class="mono col-ms">{{ fmtChars(w.output_chars) }}</td>
                  <td class="mono col-ms">{{ fmtScore(w.avg_score) }}</td>
                  <td class="mono col-ms" :class="{ 'cq-bad': w.open_errors }">{{ w.open_errors }}</td>
                </tr>
              </tbody>
            </table>
          </div>
          <div v-if="cqNodes.length" class="card-body cq-nodes">
            <div class="cq-sub">最耗时的节</div>
            <div class="table-wrap">
              <table class="req-table">
                <thead>
                  <tr>
                    <th>节</th>
                    <th>作品</th>
                    <th class="col-ms">调用</th>
                    <th class="col-ms">失败</th>
                    <th class="col-ms">耗时</th>
                    <th class="col-ms">均分</th>
                    <th class="col-ms">硬告警</th>
                  </tr>
                </thead>
                <tbody>
                  <tr v-for="n in cqNodes" :key="n.node_id">
                    <td class="path-cell" :title="n.node_path || n.node_id">{{ n.node_title || n.node_id }}</td>
                    <td class="path-cell" :title="n.work_id">{{ n.work_title || n.work_id }}</td>
                    <td class="mono col-ms">{{ n.runs }}</td>
                    <td class="mono col-ms" :class="{ 'cq-bad': n.failed }">{{ n.failed }}</td>
                    <td class="mono col-ms">{{ fmtDur(n.elapsed_ms) }}</td>
                    <td class="mono col-ms">{{ fmtScore(n.avg_score) }}</td>
                    <td class="mono col-ms" :class="{ 'cq-bad': n.open_errors }">{{ n.open_errors }}</td>
                  </tr>
                </tbody>
              </table>
            </div>
            <p class="hint-line">
              只列耗时最长的 {{ cqNodes.length }} 节；全书级任务（大纲、摘要）只进上面的表。
            </p>
          </div>
        </section>

        <!-- 质量反馈环：只讲已经落库的行为——AI 生成被采纳了多少，采纳后活下来多少 -->
        <section class="card">
          <div class="card-head">
            <span class="card-title">质量信号</span>
            <span class="card-sub">runs + 版本历史现算：采纳率＝生成被点进正文的比例；存活率＝采纳的 AI 版本没被人重写的比例</span>
          </div>
          <div class="card-body">
            <div class="q-stats">
              <span class="q-stat">
                采纳率 <b>{{ fmtPct(fbAdopt.rate) }}</b>
                （{{ fbAdopt.adopted || 0 }}/{{ fbAdopt.total || 0 }} 次）
              </span>
              <span class="q-stat">
                存活率 <b>{{ fmtPct(fbSurvive.rate) }}</b>
                （{{ fbSurvive.untouched || 0 }}/{{ fbSurvive.checked || 0 }} 版原样）
              </span>
            </div>
            <!-- 分组行多于一种才有意义：只有一个模型时上面的总数就是它的全部 -->
            <div v-if="fbByModel.length > 1" class="q-triage-list">
              <span v-for="m in fbByModel" :key="m.model" class="q-triage-item">
                {{ m.model }} 采纳 <b>{{ fmtPct(m.rate) }}</b> · {{ m.adopted }}/{{ m.total }} 次
              </span>
            </div>
            <div v-if="fbByTask.length > 1" class="q-triage-list">
              <span v-for="t in fbByTask" :key="t.task_type" class="q-triage-item">
                {{ t.task_type }} 采纳 <b>{{ fmtPct(t.rate) }}</b> · {{ t.adopted }}/{{ t.total }} 次
              </span>
            </div>
            <p v-if="!fbAdopt.total" class="hint-line">
              还没有可归因的生成：跑一次再点「采用」，这里就会出信号。
            </p>
            <p v-else class="hint-line">
              大纲与改写候选的采用路径不写版本行，已从分母剔除（{{ feedback.excluded_runs || 0 }} 次）；
              存活率比对最近 {{ fbSurvive.checked || 0 }} 条 AI 版本与其后第一条手改版，只挪空白算原样。
              <template v-if="feedback.window_capped">采纳率只统计最近 {{ feedback.window_runs }} 次成功生成。</template>
            </p>
          </div>
        </section>

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

        <section v-if="pubInsight" class="card">
          <div class="card-head">
            <span class="card-title">发表效果</span>
            <span class="card-sub">
              {{ pubInsight.pubs }} 条记录·阅读靠手填，可能不全
            </span>
          </div>
          <div class="card-body">
            <p v-if="!pubInsight.pubs" class="hint-line">
              还没记过发布台账。发完一篇回来补一条阅读/在看，这里才看得出效果。
            </p>
            <template v-else>
              <p class="ins-sum">
                共 {{ pubInsight.works }} 篇作品 · {{ pubInsight.pubs }} 次发布 · 平均阅读
                <strong>{{ pubInsight.reads_avg }}</strong> · 平均在看 <strong>{{ pubInsight.likes_avg }}</strong>
              </p>
              <p v-if="pubInsight.trend" class="hint-line">{{ trendLine }}。</p>
              <div v-if="pubInsight.by_subtype.length > 1" class="table-wrap">
                <table class="req-table">
                  <thead>
                    <tr>
                      <th>文体</th>
                      <th>篇数</th>
                      <th>平均阅读</th>
                      <th>平均在看</th>
                    </tr>
                  </thead>
                  <tbody>
                    <tr v-for="t in pubInsight.by_subtype" :key="t.subtype">
                      <td class="path-cell">{{ t.subtype }}</td>
                      <td class="mono">{{ t.pubs }}</td>
                      <td class="mono">{{ t.reads_avg }}</td>
                      <td class="mono">{{ t.likes_avg }}</td>
                    </tr>
                  </tbody>
                </table>
                <p class="hint-line">哪类文体读者买账，看平均阅读那一列（样本少时参考就好）。</p>
              </div>
            </template>
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

        <section class="card">
          <div class="card-head">
            <span class="card-title">库与快照</span>
            <span class="card-sub">每天第一次改动后自动落一份整库快照，留最近 {{ snapshotKeep }} 份</span>
            <span class="head-spacer"></span>
            <span class="mono card-path">{{ snapshotDir }}</span>
            <button class="btn btn-sm" :disabled="snapshotBusy" @click="takeSnapshot">
              {{ snapshotBusy ? '快照中…' : '立即快照' }}
            </button>
          </div>
          <div class="card-body">
            <p v-if="snapshotNote" class="hint-line">{{ snapshotNote }}</p>
            <div v-for="s in recentSnapshots" :key="s.name" class="bak-line">
              <span class="mono bak-name">{{ s.name }}</span>
              <span class="bak-meta">{{ fmtStamp(s.created_at) }} · {{ fmtBytes(s.bytes) }}</span>
            </div>
            <p v-if="!snapshotBackups.length" class="hint-line">
              还没有快照：写过字之后服务会在半小时内自己取一份，赶时间就点上面的按钮。
            </p>
            <p v-else-if="snapshotBackups.length > recentSnapshots.length" class="hint-line">
              共 {{ snapshotBackups.length }} 份，只列最近 {{ recentSnapshots.length }} 份；
              要拿某一份盖回去，先停服务再用 `scripts/restore_backup.py`。
            </p>
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

        <!-- 模板库：模板定骨架，渠道定节奏，文风定语气，三者正交 -->
        <section class="card">
          <div class="card-head">
            <span class="card-title">模板库</span>
            <span class="card-sub">{{ tplLib.length }} 套 · 模板管「写什么」，渠道与文风另算</span>
            <span class="head-spacer"></span>
            <span v-if="tplError" class="log-error">{{ tplError }}</span>
            <input
              v-model="tplQ"
              class="field field--sm tpl-search"
              placeholder="按名字搜"
              aria-label="搜模板"
              @keyup.enter="loadTemplates"
            />
            <button class="btn btn-sm" :disabled="tplBusy" @click="startCreateTemplate">新建</button>
            <button class="btn btn-sm" :disabled="tplBusy" @click="tplFile.click()">导入 JSON</button>
            <input ref="tplFile" type="file" accept="application/json,.json" hidden @change="onTplFile" />
          </div>
          <div class="card-body">
            <!-- 新建与编辑共用一份表单：字段就是模板表那几列，不另开弹层 -->
            <div v-if="tplForm.open" class="tpl-form">
              <div class="tpl-form-row">
                <label class="tpl-fl">
                  <span>名字</span>
                  <input v-model="tplForm.name" class="field field--sm" placeholder="历史专题 · 六段" />
                </label>
                <label class="tpl-fl">
                  <span>文种</span>
                  <select v-model="tplForm.kind" class="field field--sm" :disabled="Boolean(tplForm.id)">
                    <option value="article">文档</option>
                    <option value="novel">长篇</option>
                  </select>
                </label>
                <label class="tpl-fl">
                  <span>文体</span>
                  <input v-model="tplForm.subtype" class="field field--sm" placeholder="history / tech，可空" />
                </label>
                <label class="tpl-fl">
                  <span>渠道</span>
                  <select v-model="tplForm.channel" class="field field--sm">
                    <option value="">不指定</option>
                    <option v-for="c in channels" :key="c.id" :value="c.id">{{ c.label }}</option>
                  </select>
                </label>
              </div>
              <label class="tpl-fl">
                <span>什么时候用（note）</span>
                <input v-model="tplForm.note" class="field field--sm"
                       placeholder="比如：三千字以上的历史深稿，要带史料出处" />
              </label>
              <label class="tpl-fl">
                <span>骨架：每行一节，写成「标题 | 要点 | 目标字数」；已解析 {{ tplFormSections.length }} 节</span>
                <textarea v-model="tplForm.outlineText" class="field" rows="6"
                          placeholder="开场：一个具体场景 | 用一个反常识的细节抓人 | 400" />
              </label>
              <label class="tpl-fl">
                <span>文风提示（style_prompt，可空）</span>
                <textarea v-model="tplForm.stylePrompt" class="field" rows="2" />
              </label>
              <div class="tpl-form-act">
                <button class="btn btn-sm btn-primary" :disabled="tplBusy" @click="saveTemplateForm">
                  {{ tplForm.id ? '保存修改' : '新建' }}
                </button>
                <button class="btn btn-sm" @click="tplForm.open = false">取消</button>
              </div>
            </div>

            <div class="table-wrap">
              <table class="req-table">
                <thead>
                  <tr>
                    <th>名字</th>
                    <th>文种</th>
                    <th>渠道</th>
                    <th>来源</th>
                    <th class="col-ms">节数</th>
                    <th class="col-ms">用过</th>
                    <th>操作</th>
                  </tr>
                </thead>
                <tbody>
                  <tr v-for="t in tplLib" :key="t.id">
                    <td class="path-cell" :title="t.note || t.name">
                      <span v-if="t.pinned" class="tpl-star" title="已收藏">*</span>
                      <span v-if="t.is_default" class="tpl-def">默认</span>
                      {{ t.name }}
                    </td>
                    <td class="mono">
                      {{ kindText(t.kind) }}<template v-if="t.subtype">/{{ t.subtype }}</template>
                    </td>
                    <td>{{ t.channel ? channelText(t.channel) : '—' }}</td>
                    <td>{{ t.is_builtin ? '内置' : (t.source_work_id ? '来自作品' : '自建') }}</td>
                    <td class="mono col-ms">{{ (t.outline || []).length }}</td>
                    <td class="mono col-ms">{{ t.use_count || 0 }}</td>
                    <td class="act-cell">
                      <button class="mini-link" @click="duplicateTpl(t)">克隆</button>
                      <button v-if="!t.is_builtin" class="mini-link" @click="startEditTemplate(t)">编辑</button>
                      <button class="mini-link" @click="setDefaultTpl(t)">设为默认</button>
                      <button class="mini-link" @click="togglePin(t)">{{ t.pinned ? '取消收藏' : '收藏' }}</button>
                      <button v-if="!t.is_builtin" class="mini-link" @click="deleteTpl(t)">删除</button>
                      <button class="mini-link" @click="exportTpl(t)">导出</button>
                    </td>
                  </tr>
                </tbody>
              </table>
              <div v-if="!tplLib.length" class="empty-block">
                <AppIcon name="layers" :size="18" />
                <span v-if="tplQ">没有匹配的模板</span>
                <span v-else>模板库是空的：内置模板由 content-types.yaml 播种，检查服务端能否读到</span>
              </div>
            </div>
            <p v-if="tplNote" class="hint-line">{{ tplNote }}</p>
            <p class="hint-line">
              内置模板只读（每次启动按 YAML 覆盖）：要改先「克隆」，改副本。
              渠道与文风在写作台右栏的「模板 / 文风」页里选，不在这张表里。
            </p>
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
import { decodeTemplate, outlineToText, textToOutline } from '../utils/template'
import AppNav from '../components/AppNav.vue'
import AppIcon from '../components/AppIcon.vue'
import CompatDialog from '../components/CompatDialog.vue'
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
// 生成成本：整块从 /studio/stats/cost-quality 来，不在前端另算一遍
const costQuality = ref(null)
// 质量反馈环：采纳率与存活率从 /studio/feedback 现算，跟成本同一条刷新链
const feedback = ref(null)
const pubSummary = ref([])
// 发表效果：同一本台账的聚合视角，与台账同一条刷新链；拉不到就整卡收起
const pubInsight = ref(null)
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
// ---- 库快照：与删除副本同一条保留策略，所以手动按多了会挤掉最旧的 ----
const snapshotBackups = ref([])
const snapshotKeep = ref(7)
const snapshotDir = ref('')
const snapshotBusy = ref(false)
const snapshotNote = ref('')
const restoringBackup = ref(false)
const dataError = ref('')
const dataNote = ref('')
const dataBusy = ref(false)

// ---- 配置 ----
const settings = ref({})
const contentTypes = ref([])
// 渠道与文种平级（channels 段）：模板表格里把 wechat 显示成「公众号」
const channels = ref([])
const configError = ref('')
const reloadNote = ref('')
const reloadBad = ref(false)
const reloading = ref(false)

// ---- 模板库 ----
// 模板 = 骨架（写什么）；渠道 = 平台节奏。两者正交，这张表只管模板本身
const tplLib = ref([])
const tplQ = ref('')
const tplBusy = ref(false)
const tplNote = ref('')
const tplError = ref('')
const tplFile = ref(null)
const tplForm = ref({
  open: false, id: '', kind: 'article', name: '', subtype: '',
  channel: '', note: '', stylePrompt: '', outlineText: '',
})
// 骨架文本实时解析成本数：编辑时看不见「现在几节」，改着改着就删没了
const tplFormSections = computed(() => textToOutline(tplForm.value.outlineText))

const readyCount = computed(() => providers.value.filter(p => (p.compat ? p.ready : p.has_profile)).length)
const totalModels = computed(() => providers.value.reduce((n, p) => n + (p.models?.length || 0), 0))

// ---- 直连通道（compat）：卡片展示与配置弹窗 ----
const compatOpen = ref(false)
const compatEntry = ref(null)
const testingProvider = ref('')
const compatTestText = ref({})
const compatTestBad = ref({})

// key 状态四档与后端 _compat_brief 的 key_source 对齐；只报掩码与变量名，明文不出后端
function keyLine(p) {
  if (p.key_source === 'db') return `key 已配置 ${p.key_masked || ''}`
  if (p.key_source === 'env') return `环境变量 ${p.key_env} 已设置`
  if (p.key_source === 'none') return `环境变量 ${p.key_env} 未设置`
  return '无需 key'
}

function openCompatDialog(p) {
  // p 为 null = 新增；卡片来自 /admin/status，字段够弹窗初始化（models 是 id 数组）
  compatEntry.value = p ? { ...p } : null
  compatOpen.value = true
}

async function onCompatSaved() {
  compatOpen.value = false
  // 保存即生效（后端已重载配置）：重拉状态与模型列表，对话下拉立刻能看到新模型
  await Promise.allSettled([loadStatus(), providerStore.fetchModels()])
  reloadMsg.value = '直连通道已保存并生效'
  setTimeout(() => (reloadMsg.value = ''), 3000)
}

async function testCompat(p) {
  testingProvider.value = p.provider
  try {
    const r = await apiFetch('/admin/compat/' + encodeURIComponent(p.provider) + '/test', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({}),
    })
    const j = await r.json().catch(() => null)
    if (j && j.ok) {
      compatTestBad.value = { ...compatTestBad.value, [p.provider]: false }
      compatTestText.value = { ...compatTestText.value, [p.provider]: `通了 · ${j.latency_ms} ms` }
    } else {
      const text = j && typeof j.detail === 'string' ? j.detail : 'HTTP ' + r.status
      compatTestBad.value = { ...compatTestBad.value, [p.provider]: true }
      compatTestText.value = { ...compatTestText.value, [p.provider]: text }
    }
  } catch (e) {
    compatTestBad.value = { ...compatTestBad.value, [p.provider]: true }
    compatTestText.value = { ...compatTestText.value, [p.provider]: e.message }
  } finally {
    testingProvider.value = ''
  }
}

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
const cqOverall = computed(() => costQuality.value?.overall || {})
const cqWorks = computed(() => costQuality.value?.works || [])
const cqNodes = computed(() => costQuality.value?.nodes || [])
const cqByTask = computed(() => costQuality.value?.by_task || [])
const fbAdopt = computed(() => feedback.value?.adopt || {})
const fbSurvive = computed(() => feedback.value?.survive || {})
const fbByModel = computed(() => feedback.value?.by_model || [])
const fbByTask = computed(() => feedback.value?.by_task || [])

function workName(id) {
  if (!id) return '—'
  return works.value.find(w => w.id === id)?.title || id
}

async function refreshData() {
  dataBusy.value = true
  dataError.value = ''
  try {
    await Promise.all([loadWorks(), loadPubSummary(), loadInsight(), loadBackups(), loadCostQuality(), loadFeedback()])
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

async function loadInsight() {
  try {
    const r = await apiFetch('/studio/publications/insight')
    if (!r.ok) throw new Error('HTTP ' + r.status)
    pubInsight.value = await r.json()
  } catch (e) {
    // 与分析同源的是手填台账：拉不到就只收这一张卡，不挡发布台账主表
    pubInsight.value = null
  }
}

const trendLine = computed(() => {
  const t = pubInsight.value?.trend
  if (!t) return ''
  if (t.prev_avg <= 0) return `最近 5 篇均读 ${t.recent_avg}（此前均读为零，百分比不计）`
  const pct = Math.round(((t.recent_avg - t.prev_avg) / t.prev_avg) * 100)
  return `最近 5 篇均读 ${t.recent_avg}，比前 5 篇的 ${t.prev_avg} ${pct >= 0 ? '涨' : '跌'} ${Math.abs(pct)}%`
})

async function loadCostQuality() {
  try {
    const r = await apiFetch('/studio/stats/cost-quality?works=30&nodes=30')
    if (!r.ok) throw new Error('HTTP ' + r.status)
    costQuality.value = await r.json()
  } catch (e) {
    // 拿不到就清空：宁可显示「还没有生成记录」，也不摆一份过期账
    costQuality.value = null
    dataError.value = '生成成本不可用：' + e.message
  }
}

async function loadFeedback() {
  try {
    const r = await apiFetch('/studio/feedback')
    if (!r.ok) throw new Error('HTTP ' + r.status)
    feedback.value = await r.json()
  } catch (e) {
    // 与成本一样：拉不到就清空，不留一份看着真、实则过期的信号
    feedback.value = null
    dataError.value = '质量信号不可用：' + e.message
  }
}

async function loadBackups() {
  try {
    const r = await apiFetch('/studio/backups')
    if (!r.ok) throw new Error('HTTP ' + r.status)
    const j = await r.json()
    nodeBackups.value = j.nodes || []
    workBackups.value = j.works || []
    snapshotBackups.value = j.snapshots || []
    snapshotKeep.value = j.snapshot_keep || 7
    snapshotDir.value = j.snapshot_dir || ''
    backupDir.value = j.dir || ''
  } catch (e) {
    dataError.value = '备份列表不可用：' + e.message
  }
}

// 列表只给 5 行：这一页要回答的是「最近有没有在取」，不是备份档案室。
// 真需要翻旧的那一份，`scripts/restore_backup.py --list` 才是完整清单。
const recentSnapshots = computed(() => snapshotBackups.value.slice(0, 5))

async function takeSnapshot() {
  snapshotBusy.value = true
  snapshotNote.value = ''
  dataError.value = ''
  try {
    const r = await apiFetch('/studio/backups/snapshot', { method: 'POST' })
    if (!r.ok) {
      dataError.value = await errLine(r, '快照失败')
      return
    }
    const j = await r.json()
    snapshotNote.value = `已存下 ${j.name}（${fmtBytes(j.bytes)}）`
    await loadBackups()
  } catch (e) {
    dataError.value = '快照失败：' + e.message
  } finally {
    snapshotBusy.value = false
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
    await Promise.all([loadPubs(), loadPubSummary(), loadInsight()])
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
  await Promise.all([loadPubs(), loadPubSummary(), loadInsight()])
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
  await Promise.all([loadSettings(), loadContentTypes(), loadTemplates()])
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
  // 渠道与文种平级（新 YAML 的 channels 段），模板表格里要把 wechat 显示成「公众号」
  channels.value = j.channels || []
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

// ---- 模板库：模板 = 骨架（写什么），渠道与文风是另外两个维度 ----

function kindText(kind) {
  return kind === 'novel' ? '长篇' : '文档'
}

function channelText(id) {
  return channels.value.find((c) => c.id === id)?.label || id
}

async function loadTemplates() {
  tplError.value = ''
  const p = new URLSearchParams()
  if (tplQ.value.trim()) p.set('q', tplQ.value.trim())
  const r = await apiFetch('/studio/templates?' + p.toString())
  if (!r.ok) {
    tplError.value = await errLine(r, '模板库读取失败')
    return
  }
  tplLib.value = (await r.json()).map(decodeTemplate)
}

function startCreateTemplate() {
  tplForm.value = {
    open: true, id: '', kind: 'article', name: '', subtype: '',
    channel: '', note: '', stylePrompt: '', outlineText: '',
  }
  tplNote.value = ''
}

function startEditTemplate(t) {
  tplForm.value = {
    open: true,
    id: t.id,
    kind: t.kind || 'article',
    name: t.name || '',
    subtype: t.subtype || '',
    channel: t.channel || '',
    note: t.note || '',
    stylePrompt: t.style_prompt || '',
    // 骨架折成一行一节：直接编 JSON 容易一个逗号写错整份存不进去
    outlineText: outlineToText(t.outline),
  }
  tplNote.value = ''
}

async function saveTemplateForm() {
  const f = tplForm.value
  if (!f.name.trim()) {
    tplNote.value = '名字不能为空'
    return
  }
  const outline = textToOutline(f.outlineText)
  if (!outline.length) {
    tplNote.value = '骨架不能为空：至少一节，没有骨架时 S0 不出内容'
    return
  }
  tplBusy.value = true
  try {
    const payload = {
      name: f.name.trim(),
      subtype: f.subtype.trim(),
      channel: f.channel,
      note: f.note.trim(),
      style_prompt: f.stylePrompt.trim(),
      outline,
    }
    // kind 只在新建时发：PATCH 不改文种（换了文种，骨架的字数节奏对不上）
    if (!f.id) payload.kind = f.kind
    const r = await apiFetch(f.id ? `/studio/templates/${f.id}` : '/studio/templates', {
      method: f.id ? 'PATCH' : 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    })
    if (!r.ok) {
      tplNote.value = await errLine(r, f.id ? '保存失败' : '新建失败')
      return
    }
    tplNote.value = f.id ? '已保存' : `已新建「${payload.name}」`
    tplForm.value = { ...tplForm.value, open: false, id: '' }
    await loadTemplates()
  } finally {
    tplBusy.value = false
  }
}

async function duplicateTpl(t) {
  tplBusy.value = true
  try {
    const r = await apiFetch(`/studio/templates/${t.id}/duplicate`, { method: 'POST' })
    if (!r.ok) {
      tplNote.value = await errLine(r, '克隆失败')
      return
    }
    const got = decodeTemplate(await r.json())
    tplNote.value = `已克隆为「${got.name}」，改它不影响原模板`
    await loadTemplates()
  } finally {
    tplBusy.value = false
  }
}

async function setDefaultTpl(t) {
  const r = await apiFetch(`/studio/templates/${t.id}/default`, { method: 'POST' })
  if (!r.ok) {
    tplNote.value = await errLine(r, '设为默认失败')
    return
  }
  const scope = `${kindText(t.kind)}${t.subtype ? '/' + t.subtype : ''}${t.channel ? '/' + channelText(t.channel) : ''}`
  tplNote.value = `以后新建 ${scope} 且没指定模板时用它`
  await loadTemplates()
}

async function togglePin(t) {
  const r = await apiFetch(`/studio/templates/${t.id}/pin`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ pinned: !Number(t.pinned || 0) }),
  })
  if (!r.ok) {
    tplNote.value = await errLine(r, '收藏失败')
    return
  }
  await loadTemplates()
}

async function deleteTpl(t) {
  if (!window.confirm(`删掉模板「${t.name}」？引用它的作品会被解绑（作品本身不删），之后按同组默认模板走。`)) return
  const r = await apiFetch(`/studio/templates/${t.id}`, { method: 'DELETE' })
  if (!r.ok) {
    tplNote.value = await errLine(r, '删除失败')
    return
  }
  const got = await r.json()
  tplNote.value = got.unbound ? `已删除，${got.unbound} 本作品被解绑` : '已删除'
  await loadTemplates()
}

async function exportTpl(t) {
  const r = await apiFetch(`/studio/templates/${t.id}/export`)
  if (!r.ok) {
    tplNote.value = await errLine(r, '导出失败')
    return
  }
  // 带口令的下载：fetch 取 blob 再点一个临时 <a>，不能直接 href
  const url = URL.createObjectURL(await r.blob())
  const a = document.createElement('a')
  a.href = url
  a.download = `${t.name || 'template'}.template.json`
  document.body.appendChild(a)
  a.click()
  a.remove()
  URL.revokeObjectURL(url)
}

async function onTplFile(ev) {
  const file = ev.target.files?.[0]
  ev.target.value = '' // 选同一个文件两次也要能触发 change
  if (!file) return
  let data
  try {
    data = JSON.parse(await file.text())
  } catch {
    tplNote.value = '这不是 JSON 文件：选错了或文件坏了'
    return
  }
  const r = await apiFetch('/studio/templates/import', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(data),
  })
  if (!r.ok) {
    tplNote.value = await errLine(r, '导入失败')
    return
  }
  const got = decodeTemplate(await r.json())
  tplNote.value = `已导入「${got.name}」`
  await loadTemplates()
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

// 生成账的三种度量：耗时、字符数、评分（null = 没评过，不显示成 0）
function fmtDur(ms) {
  const v = Number(ms) || 0
  if (!v) return '—'
  if (v < 1000) return v + 'ms'
  if (v < 60000) return (v / 1000).toFixed(1) + 's'
  return `${Math.floor(v / 60000)}分${Math.round((v % 60000) / 1000)}秒`
}

function fmtChars(n) {
  const v = Number(n) || 0
  if (!v) return '0'
  return v >= 10000 ? (v / 10000).toFixed(1) + '万字' : v + '字'
}

function fmtScore(v) {
  return v == null ? '—' : Number(v).toFixed(1)
}

// 采纳率/存活率都是 0~1 的小数，折成百分比看；空值显「—」不当 0% 骗人
function fmtPct(v) {
  if (v == null) return '—'
  return Math.round(Number(v) * 100) + '%'
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
/* 直连通道卡片：地址、key 状态、操作行——都是信息行不是链接，字号对齐既有 pc-* 惯例 */
.pc-base {
  margin: 0;
  font-size: 11px;
  color: var(--text-muted);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.pc-keyline {
  margin: 0 0 2px;
  font-size: 11px;
  color: var(--text-faint);
}
.pc-keyline.is-bad {
  color: var(--danger-fg);
}
.pc-actions {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 10px;
  margin-top: auto;
}
.pc-actions .pc-link {
  margin-top: 0;
}
.pc-link:disabled {
  opacity: 0.55;
  cursor: default;
}
.pc-test-note {
  font-size: 11px;
  color: var(--run-fg);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  max-width: 100%;
}
.pc-test-note.is-bad {
  color: var(--danger-fg);
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

/* ---------- 生成成本与质量 ---------- */
.cq-tasks {
  margin-top: 10px;
}
.cq-bad {
  color: var(--danger-fg);
}
.cq-nodes {
  border-top: 1px solid var(--line-soft);
}
.cq-sub {
  margin-bottom: 8px;
  font-size: 12px;
  font-weight: 600;
  color: var(--text-muted);
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
.ins-sum {
  margin: 0 0 8px;
  font-size: 12.5px;
  color: var(--text-muted);
}
.ins-sum strong {
  font-family: theme('fontFamily.mono');
  color: var(--text);
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

/* ---------- 模板库 ---------- */
.tpl-search {
  width: 140px;
}
.tpl-form {
  display: flex;
  flex-direction: column;
  gap: 8px;
  padding: 10px;
  margin-bottom: 12px;
  border: 1px solid var(--line-soft);
  border-radius: 7px;
  background: var(--panel-2);
}
.tpl-form-row {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
}
.tpl-form-row .tpl-fl {
  flex: 1 1 120px;
}
.tpl-fl {
  display: flex;
  flex-direction: column;
  gap: 3px;
  font-size: 11px;
  color: var(--text-faint);
}
.tpl-form-act {
  display: flex;
  gap: 8px;
}
/* 收藏与默认是行上两个标记：挤在名字前面，扫一眼就知道哪套在用 */
.tpl-star {
  margin-right: 4px;
  color: var(--warn-fg);
}
.tpl-def {
  margin-right: 4px;
  padding: 0 5px;
  border-radius: 4px;
  background: rgba(var(--run-rgb), 0.16);
  color: var(--run-fg);
  font-size: 10.5px;
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
