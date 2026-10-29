<template>
  <div class="app-shell">
    <AppNav />

    <!-- 会自己消掉的小提示：上下移的结果、删除被备份拦住都走这里（书架与工作台共用） -->
    <div v-if="notice" class="notice" :class="{ 'notice--bad': noticeBad }" role="status">
      {{ notice }}
    </div>

    <div class="studio">
      <!-- 未选作品：书架 -->
      <main v-if="!store.work" ref="shelfEl" class="shelf">
        <header class="shelf-head">
          <div>
            <h1>写作台</h1>
            <p class="shelf-sub">长篇连载与单篇文档共用一套工作台，结构由作品类型决定。</p>
          </div>
          <div class="shelf-tools">
            <label class="shelf-search">
              <AppIcon name="search" :size="14" />
              <input v-model="shelfQ" class="field" placeholder="搜标题 / 主旨" aria-label="筛选作品" />
            </label>
            <button class="btn btn-sm" title="跨作品搜正文与素材（Ctrl+K）" @click="openSearch()">
              <AppIcon name="search" :size="13" /> 搜正文
            </button>
            <button class="btn btn-primary" @click="showCreate = !showCreate">
              <AppIcon name="plus" :size="15" /> 新建作品
            </button>
          </div>
        </header>

        <!-- 归档只是从书架收起，不删任何东西：所以在写/已归档是两个视图，不是筛选器 -->
        <div class="shelf-views">
          <button class="view-chip" :class="{ 'is-on': !showArchived }" @click="showArchived = false">
            在写 {{ activeWorks.length }}
          </button>
          <button class="view-chip" :class="{ 'is-on': showArchived }" @click="showArchived = true">
            已归档 {{ archivedWorks.length }}
          </button>
        </div>

        <!-- 写作日历：数据从版本表现算，不在别处另记一份 -->
        <section class="write-log" aria-label="写作统计">
          <span class="wl-item"><strong>{{ fmt(store.dailyWrites.today) }}</strong> 今天字数</span>
          <span class="wl-item"><strong>{{ store.dailyWrites.streak }}</strong> 连续天</span>
          <span class="wl-days" :title="last14.map((d) => `${d.day} ${d.chars} 字`).join(' / ')">
            <span
              v-for="d in last14"
              :key="d.day"
              class="wl-bar"
              :class="{ 'is-on': d.chars > 0 }"
              :style="{ height: barHeight(d.chars) }"
            ></span>
          </span>
        </section>

        <form v-if="showCreate" class="create card" @submit.prevent="create">
          <div class="create-row">
            <label class="fl">
              <span>类型</span>
              <select v-model="form.kind" class="field" @change="onKindChange">
                <option v-for="k in kinds" :key="k.id" :value="k.id">{{ k.label }}</option>
              </select>
            </label>
            <label v-if="scales.length > 1" class="fl">
              <span>规模</span>
              <select v-model="form.scale" class="field" @change="applyScaleDefaults">
                <option v-for="s in scales" :key="s.id" :value="s.id">{{ s.label }}</option>
              </select>
            </label>
            <label v-if="form.kind === 'novel'" class="fl">
              <span>题材</span>
              <input v-model="form.subtype" class="field" placeholder="玄幻 / 都市 / 科幻…" />
            </label>
            <label v-else-if="subtypes.length" class="fl">
              <span>文体</span>
              <select v-model="form.subtype" class="field">
                <option v-for="s in subtypes" :key="s.id" :value="s.id">{{ s.label }}</option>
              </select>
            </label>
            <label class="fl grow">
              <span>标题</span>
              <input v-model="form.title" class="field" required placeholder="作品标题" />
            </label>
          </div>
          <label class="fl grow">
            <span>一句话主旨</span>
            <textarea v-model="form.premise" class="field" rows="2"
                      placeholder="这篇/这本书要讲什么，给谁看，最重要的一句话是什么" />
          </label>
          <div class="create-foot">
            <label class="fl small">
              <span>目标字数</span>
              <input v-model.number="form.target_words" class="field" type="number" min="0" step="1000" />
            </label>
            <p v-if="store.error" class="err">{{ store.error }}</p>
            <button class="btn btn-primary" type="submit" :disabled="creating">
              {{ creating ? '创建中…' : '创建并进入' }}
            </button>
          </div>
        </form>

        <!-- 灵感箱：一句话想法先落这儿，不占作品 -->
        <section class="ideas">
          <div class="ideas-head">
            <h2>灵感箱</h2>
            <span class="ideas-sub">还没成形的一句话，先记着；「用它开写」会填进上面的新建表单</span>
          </div>
          <form class="ideas-add" @submit.prevent="saveIdea">
            <input v-model="ideaDraft" class="field" placeholder="一句话：想写什么，给谁看" />
            <button class="btn btn-sm" type="submit" :disabled="ideaBusy || !ideaDraft.trim()">记下</button>
          </form>
          <ul v-if="store.ideas.length" class="idea-list">
            <li v-for="i in store.ideas" :key="i.id" class="idea-row">
              <span class="idea-text" :title="i.text">{{ i.text }}</span>
              <button class="link-btn" @click="startFromIdea(i)">用它开写</button>
              <button class="idea-del" title="删掉这条想法" @click="store.deleteIdea(i.id)">删</button>
            </li>
          </ul>
        </section>

        <div v-if="visibleWorks.length" class="grid">
          <div
            v-for="w in visibleWorks"
            :key="w.id"
            class="work-card"
            role="button"
            tabindex="0"
            @click="openWork(w.id)"
            @keydown.enter="openWork(w.id)"
          >
            <button
              class="work-arch"
              :title="showArchived ? '移回「在写」' : '归档：从书架收起，正文与版本都不删'"
              @click.stop="showArchived ? unarchiveWork(w) : archiveWork(w)"
            >
              <AppIcon :name="showArchived ? 'refresh' : 'archive'" :size="12" />
            </button>
            <button
              class="work-del"
              title="删除作品（连同节点树与全部历史版本；删前留一份整库副本）"
              @click.stop="removeWork(w)"
            >
              <AppIcon name="trash" :size="12" />
            </button>
            <span class="work-kind">{{ kindLabel(w) }}</span>
            <span class="work-title">{{ w.title }}</span>
            <span class="work-meta">{{ w.node_count }} 节 · {{ fmt(w.words) }} 字</span>
            <span v-if="pubOf(w.id)" class="work-pub">
              已发 {{ pubOf(w.id).pubs }} 篇 · 阅读 {{ fmt(pubOf(w.id).reads) }}
            </span>
            <span class="work-premise">{{ w.premise || '还没有主旨' }}</span>
          </div>
        </div>
        <p v-else-if="!showCreate" class="shelf-empty">
          <template v-if="shelfQ">
            没有匹配「{{ shelfQ }}」的作品。
            <button class="link-btn" @click="openSearch(shelfQ)">去正文里搜</button>
          </template>
          <template v-else-if="showArchived">还没有归档的作品。归档只是从书架收起，不删正文。</template>
          <template v-else-if="archivedWorks.length">
            在写的都归档了（{{ archivedWorks.length }} 篇），切到「已归档」可以翻出来。
          </template>
          <template v-else>
            还没有作品。先点右上角「新建作品」，进去之后左栏有「导入旧稿」，能把已有章节一次贴进来。
          </template>
        </p>
      </main>

      <!-- 已选作品：三栏工作台 -->
      <template v-else>
        <StudioTree
          :tree="store.tree"
          :active-id="store.activeNodeId"
          :title="store.work.title"
          :stats="store.stats"
          :target="store.work.target_words || 0"
          :work-id="store.work.id"
          @select="onSelectNode"
          @create="quickCreate"
          @remove="removeNode"
          @move="moveNode"
          @import="importOpen = true"
        />

        <main class="editor">
          <header class="ed-top">
            <button class="icon-btn" title="返回书架" @click="backToShelf">
              <AppIcon name="library" :size="16" />
            </button>
            <div class="ed-work-wrap">
              <button class="ed-work" title="改作品标题与主旨" @click="toggleWorkEdit">
                <span class="ed-work-name">{{ store.work.title }}</span>
                <AppIcon name="pen" :size="12" />
              </button>
              <div v-if="workEditOpen" class="work-pop">
                <label class="fl">
                  <span>作品标题</span>
                  <input
                    v-model="workTitleDraft"
                    class="field"
                    placeholder="这篇叫什么"
                    @keydown.enter.prevent="saveWorkMeta"
                  />
                </label>
                <label class="fl">
                  <span>一句话主旨</span>
                  <textarea v-model="workPremiseDraft" class="field" rows="3"
                            placeholder="这篇要替读者讲清楚什么"></textarea>
                </label>
                <label class="fl">
                  <span>公众号摘要</span>
                  <textarea v-model="workDigestDraft" class="field" rows="3"
                            placeholder="≤120 字；不填的话公众号会截正文前 54 字"></textarea>
                </label>
                <div class="work-pop-row">
                  <button class="btn btn-sm" :disabled="digestBusy" @click="makeDigestInPop">
                    {{ digestBusy ? '生成中…' : 'AI 写摘要' }}
                  </button>
                  <span class="work-pop-tip inline">{{ workDigestDraft.length }} 字</span>
                </div>
                <p class="work-pop-tip">主旨进每一轮生成的上下文，改完下一节就生效。</p>

                <!-- 发布台账：公众号没有接口，链接与阅读数只能手填；
                     记下来书架会显示，文末「往期推荐」也从这里取 -->
                <details v-if="!store.isNovel" class="pub-box">
                  <summary>发布记录（{{ store.publications.length }}）</summary>
                  <div v-for="p in store.publications" :key="p.id" class="pub-row">
                    <span class="pub-date">{{ p.published_at || '未填日期' }}</span>
                    <span class="pub-num">阅读 {{ fmt(p.reads) }} · 在看 {{ fmt(p.likes) }}</span>
                    <a v-if="p.url" class="pub-link" :href="p.url" target="_blank" rel="noreferrer">链接</a>
                    <button class="pub-del" @click="removePub(p)">删</button>
                  </div>
                  <p v-if="!store.publications.length" class="work-pop-tip">
                    发出去之后把链接和阅读数记在这里。
                  </p>
                  <div class="pub-form">
                    <input v-model="pubDraft.published_at" class="field" placeholder="2026-10-01" />
                    <input v-model="pubDraft.url" class="field" placeholder="文章链接（可空）" />
                    <input v-model.number="pubDraft.reads" class="field" type="number" placeholder="阅读" />
                    <input v-model.number="pubDraft.likes" class="field" type="number" placeholder="在看" />
                    <button class="btn btn-sm" @click="savePub">记一条</button>
                  </div>
                </details>

                <div class="work-pop-act">
                  <button class="btn btn-sm" @click="workEditOpen = false">取消</button>
                  <button
                    class="btn btn-primary btn-sm"
                    :disabled="savingWork || !workTitleDraft.trim()"
                    @click="saveWorkMeta"
                  >{{ savingWork ? '保存中…' : '保存' }}</button>
                </div>
              </div>
            </div>
            <span v-if="store.template" class="pill pill--info">{{ store.template.name }}</span>
            <span
              class="ed-save"
              :class="{ 'is-bad': saveState === 'error' }"
              :title="saveState === 'error' ? '没存上，点一下重试（正文还在页面上）' : ''"
              @click="saveState === 'error' && flushSave()"
            >{{ saveLabel }}</span>
            <div class="ed-right">
              <button class="btn btn-sm" :disabled="store.outlineBusy || store.pipeline.running"
                      title="按模板和主旨生成目录，落成节点" @click="doOutline">
                <AppIcon name="layers" :size="13" />
                {{ store.outlineBusy ? '大纲中…' : '大纲' }}
              </button>
              <button class="btn btn-sm" :disabled="store.pipeline.running"
                      title="把还没有正文的节逐节生成" @click="doPipeline">
                <AppIcon name="zap" :size="13" /> 整篇
              </button>
              <button class="btn btn-sm" :disabled="store.queueBusy"
                      title="排进队列，关页面也会继续跑" @click="doEnqueue">
                <AppIcon name="activity" :size="13" /> 排队
              </button>
              <button class="btn btn-sm" title="拼装成 Markdown 并下载" @click="store.downloadExport()">
                <AppIcon name="download" :size="13" /> 导出
              </button>
              <button class="btn btn-sm" title="公众号排版预览、发布前检查与一键复制" @click="wechatOpen = true">
                <AppIcon name="send" :size="13" /> 公众号
              </button>
              <select
                class="ed-model"
                :value="store.genModel"
                :title="modelHint"
                aria-label="生成用的模型"
                @change="store.setGenModel($event.target.value)"
              >
                <option value="">自动（后端默认）</option>
                <!-- 目录还没回来时先保住已选中的那一项，否则下拉框会显示成空白 -->
                <option v-if="store.genModel && !knownModel(store.genModel)" :value="store.genModel">
                  {{ store.genModel }}
                </option>
                <optgroup v-for="g in store.modelGroups" :key="g.key" :label="g.name">
                  <option v-for="m in g.models" :key="m.id" :value="m.id">
                    {{ m.label === m.id ? m.id : `${m.label} · ${m.id}` }}
                  </option>
                </optgroup>
              </select>
            </div>
          </header>

          <div v-if="!store.node" class="ed-empty">
            <AppIcon name="pen" :size="20" />
            <h2>选一个节点开始</h2>
            <p>{{ emptyHint }}</p>
          </div>

          <template v-else>
            <div class="ed-head">
              <input v-model="titleDraft" class="ed-title" aria-label="节点标题"
                     @change="saveTitle" placeholder="未命名" />
              <select class="field status-select" :value="store.node.status" @change="changeStatus">
                <option value="draft">草稿</option>
                <option value="review">待审</option>
                <option value="final">定稿</option>
              </select>
              <span class="ed-count" :class="{ over: nodeTarget && wordCount > nodeTarget * 1.1 }">
                {{ wordCount }} 字<template v-if="nodeTarget"> / {{ nodeTarget }}</template>
              </span>
              <span
                v-if="nodeTarget"
                class="ed-prog"
                :title="`本节目标 ${nodeTarget} 字`"
              ><i :style="{ width: Math.min(100, (wordCount / nodeTarget) * 100) + '%' }"></i></span>
            </div>

            <div class="ed-body">
              <textarea
                ref="editorEl"
                v-model="contentDraft"
                class="ed-textarea"
                spellcheck="false"
                placeholder="在这里写，或点下方「生成」让 AI 出草稿…"
                @input="onInput"
              />
            </div>

            <!-- 生成预览：先看，点采用才写回 -->
            <section v-if="store.generating || store.genText || store.genError" class="gen">
              <header class="gen-head">
                <span class="gen-title">
                  <span v-if="store.generating" class="spinner" aria-hidden="true"></span>
                  {{ genStatus }}
                </span>
                <span class="gen-meta">{{ genMetaText }}</span>
              </header>
              <!-- 装配阶段就有的告警（素材没索引、预算不够、摘要过期）原来只在「上下文」里能看到 -->
              <p v-for="(w, i) in (store.genMeta?.warnings || [])" :key="'gw' + i" class="gen-warn">{{ w }}</p>
              <p v-if="store.genMeta?.used_sources?.length" class="gen-src">
                引用素材：{{ store.genMeta.used_sources.map((s) => s.title).join('、') }}
              </p>
              <!-- 逐段勾选：一千五百字里只想要三段时，不必先整段采用再手删 -->
              <div v-if="store.genText" class="gen-text">
                <label
                  v-for="(p, i) in genParas"
                  :key="i"
                  class="gen-para"
                  :class="{ 'is-off': !isPicked(i) }"
                >
                  <input
                    v-if="!store.generating"
                    v-model="genPicked[i]"
                    type="checkbox"
                    class="gen-para-box"
                    :aria-label="`采用第 ${i + 1} 段`"
                  />
                  <span class="gen-para-text">{{ p }}</span>
                </label>
              </div>
              <p v-if="store.genError" class="gen-err">{{ store.genError }}</p>
              <!-- 停止后别把半截稿当失败：已经出来的部分可以照常采用 -->
              <p v-if="store.genStopped" class="gen-stop">
                已停止：上面是停下前生成的部分，可以继续采用
              </p>
              <footer v-if="store.generating" class="gen-actions">
                <button class="btn btn-sm" @click="store.stopGenerate()">停止生成</button>
                <span class="gen-tip">断开连接，已生成的部分会留下</span>
              </footer>
              <footer v-if="!store.generating && store.genText" class="gen-actions">
                <button class="btn btn-primary btn-sm" :disabled="!pickedCount" @click="adopt">
                  <AppIcon name="check" :size="13" />
                  {{ pickedCount === genParas.length ? '全部采用' : `采用 ${pickedCount}/${genParas.length} 段` }}
                </button>
                <button class="btn btn-sm" @click="store.discardGenerated()">放弃</button>
                <button class="link-btn" @click="pickAll(true)">全选</button>
                <button class="link-btn" @click="pickAll(false)">全不选</button>
                <span class="gen-tip">勾掉不要的段；采用会存为一个版本，可在右栏「历史」里回滚</span>
              </footer>
            </section>

            <!-- 整篇流水线进度：逐节跑，不阻塞看进度 -->
            <section v-if="showPipeline" class="pipe">
              <header class="pipe-head">
                <span class="pipe-title">
                  <span v-if="store.pipeline.running" class="spinner" aria-hidden="true"></span>
                  {{ pipeTitle }}
                </span>
                <span class="pipe-meta">
                  <template v-if="store.pipeline.model">{{ store.pipeline.model }} · </template>
                  已完成 {{ store.pipeline.done }}/{{ store.pipeline.total }} 节 · {{ store.pipeline.chars }} 字
                  <template v-if="store.pipeline.indexed"> · 索引 {{ store.pipeline.indexed }}</template>
                  <template v-if="store.pipeline.summarized"> · 摘要 {{ store.pipeline.summarized }}</template>
                </span>
                <button v-if="!store.pipeline.running" class="mini" @click="resetPipeline">收起</button>
              </header>
              <div class="bar"><div class="bar-fill" :style="{ width: pipePercent }"></div></div>
              <pre v-if="store.pipeline.preview" class="pipe-preview">{{ store.pipeline.preview }}</pre>
              <p v-if="store.pipeline.error" class="pipe-err">{{ store.pipeline.error }}</p>
            </section>

            <!-- 队列：落库的任务，断网与重启都不影响 -->
            <section v-if="showQueue" class="pipe queue">
              <header class="pipe-head">
                <span class="pipe-title">
                  <span v-if="store.queue?.current" class="spinner" aria-hidden="true"></span>
                  {{ queueTitle }}
                </span>
                <span class="pipe-meta">
                  排队 {{ qc.queued || 0 }} · 完成 {{ qc.done || 0 }}
                  <template v-if="qc.failed"> · 失败 {{ qc.failed }}</template>
                  <template v-if="store.queue?.eta_sec">
                    · 约 {{ Math.ceil(store.queue.eta_sec / 60) }} 分钟
                  </template>
                </span>
                <button v-if="store.queue?.paused" class="mini" @click="resumeQ">继续</button>
                <button v-else class="mini" @click="pauseQ">暂停</button>
                <button class="mini" @click="queueHidden = true">收起</button>
              </header>
              <p v-if="store.queue?.paused" class="pipe-err">已暂停：{{ store.queue.reason }}</p>
              <div v-if="store.queue?.current" class="bar">
                <div class="bar-fill indeterminate"></div>
              </div>

              <!-- 抽检卡点：连续两节判「重写」就整队停下 -->
              <div v-if="store.queue?.triage?.due" class="triage">
                <p class="triage-title">抽检：这两节能用吗？连续两次「重写」会暂停整条队列</p>
                <div class="triage-list">
                  <div v-for="s in store.queue.triage.samples.slice(0, 2)" :key="s.node_id" class="triage-item">
                    <span class="triage-name">{{ s.title || s.node_id }}</span>
                    <span class="triage-score">{{ s.score ?? '—' }}</span>
                  </div>
                </div>
                <div class="triage-act">
                  <button class="btn btn-sm" @click="doTriage('ok')">可用，继续</button>
                  <button class="btn btn-sm" @click="doTriage('rewrite')">要重写</button>
                </div>
              </div>
            </section>

            <footer class="gen-bar">
              <select v-model="taskType" class="field task-select">
                <option value="article_draft">写本节</option>
                <option value="continue">续写</option>
                <!-- 润色是对已有正文动手，空节选它只会让模型凭骨架瞎编（后端也会拦） -->
                <option value="polish" :disabled="!wordCount">润色{{ wordCount ? '' : '（需正文）' }}</option>
              </select>
              <!-- 提示词：单行 input 写长了只看得到尾巴，换成随内容长高的多行框 -->
              <textarea
                ref="instrEl"
                v-model="instruction"
                class="field gen-input"
                rows="2"
                placeholder="可选：这一节要写什么（大纲/整篇也用这句）。Enter 生成，Shift+Enter 换行"
                @keydown="onInstructionKeydown"
              ></textarea>
              <button class="btn btn-sm" title="对选中文字改写/压缩/扩写，多候选对比" @click="openTransform">
                <AppIcon name="pen" :size="13" /> 加工
              </button>
              <button class="btn btn-primary" :disabled="store.generating" @click="runGenerate">
                <AppIcon name="spark" :size="14" />
                {{ store.generating ? '生成中' : '生成' }}
              </button>
            </footer>
          </template>
        </main>

        <StudioPanel
          ref="panelRef"
          :task="{ task_type: taskType, instruction }"
          @locate="locateEvidence"
          @restore="onRestoreVersion"
        />
      </template>
    </div>

    <TransformDialog
      v-model:open="transformOpen"
      :text="contentDraft"
      :selection="selectedText"
      :instruction="instruction"
      @adopt="onTransformAdopt"
    />

    <WechatDialog v-model:open="wechatOpen" @goto="gotoIssue" />
    <GlobalSearch v-model:open="searchOpen" :initial="searchSeed" @goto="gotoSearchHit" />
    <ImportDocDialog
      v-model:open="importOpen"
      :parent-id="importParent.id"
      :parent-title="importParent.title"
      :leaf-kind="importKind"
      @done="onImported"
    />
  </div>
</template>

<script setup>
import { computed, nextTick, onMounted, onUnmounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import AppNav from '../components/AppNav.vue'
import AppIcon from '../components/AppIcon.vue'
import StudioTree from '../components/StudioTree.vue'
import StudioPanel from '../components/StudioPanel.vue'
import TransformDialog from '../components/TransformDialog.vue'
import WechatDialog from '../components/WechatDialog.vue'
import GlobalSearch from '../components/GlobalSearch.vue'
import ImportDocDialog from '../components/ImportDocDialog.vue'
import { useStudioStore } from '../stores/studio'
import { apiFetch } from '../utils/api'

const store = useStudioStore()
const route = useRoute()
const router = useRouter()

const showCreate = ref(false)
const creating = ref(false)
const form = ref({ kind: 'article', scale: 'single', subtype: 'tech', title: '', premise: '', target_words: 5000 })

const titleDraft = ref('')
const contentDraft = ref('')
const saveState = ref('saved') // saved | saving | dirty | error
const taskType = ref('article_draft')
const instruction = ref('')
const editorEl = ref(null)
const instrEl = ref(null)
const panelRef = ref(null)

// 类型下拉来自 /studio/content-types（content-types.yaml），新增文种只改 YAML
const kinds = computed(() =>
  store.contentTypes.length
    ? store.contentTypes
    : [{ id: 'article', label: '文档', scales: [{ id: 'single', label: '单篇' }], subtypes: [] }],
)
const currentKind = computed(() => kinds.value.find((k) => k.id === form.value.kind) || null)
const scales = computed(() => currentKind.value?.scales || [])
const subtypes = computed(() => currentKind.value?.subtypes || [])

const transformOpen = ref(false)
const selectedText = ref('')

// ---- 作品信息（标题 / 主旨）就地可改 ----
const workEditOpen = ref(false)
const workTitleDraft = ref('')
const workPremiseDraft = ref('')
const workDigestDraft = ref('')
const savingWork = ref(false)
const digestBusy = ref(false)
const wechatOpen = ref(false)

// 书架：搜索框只筛卡片，跨作品搜正文走 Ctrl+K 那个弹层——两种搜索混在一个框里会互相误会
const shelfQ = ref('')
const showArchived = ref(false)
const searchOpen = ref(false)
const searchSeed = ref('')

/** 打开全库搜索；带 seed 时（书架空态点的「去正文里搜」）直接搜这个词 */
function openSearch(seed = '') {
  searchSeed.value = seed
  searchOpen.value = true
}

const ideaDraft = ref('')
const ideaBusy = ref(false)
const pendingIdea = ref('')
const shelfEl = ref(null)

// 关掉新建表单就别再记着那条灵感：否则下一次手动新建会被误标成「用它开写」
watch(showCreate, (on) => {
  if (!on) pendingIdea.value = ''
})

async function saveIdea() {
  const text = ideaDraft.value.trim()
  if (!text || ideaBusy.value) return
  ideaBusy.value = true
  const idea = await store.addIdea(text)
  ideaBusy.value = false
  if (!idea) {
    showNotice(store.error || '记下失败', true)
    return
  }
  ideaDraft.value = ''
}

/** 用它开写：只填新建表单，kind/scale 仍由人挑——猜错类型比不填更麻烦 */
async function startFromIdea(idea) {
  pendingIdea.value = idea.id
  form.value.title = idea.text.slice(0, 40)
  form.value.premise = idea.text
  showCreate.value = true
  await nextTick()
  shelfEl.value?.scrollTo({ top: 0, behavior: 'smooth' })
}

const activeWorks = computed(() => store.works.filter((w) => (w.status || 'active') !== 'archived'))
const archivedWorks = computed(() => store.works.filter((w) => w.status === 'archived'))
const visibleWorks = computed(() => {
  const base = showArchived.value ? archivedWorks.value : activeWorks.value
  const q = shelfQ.value.trim().toLowerCase()
  if (!q) return base
  return base.filter((w) =>
    (w.title || '').toLowerCase().includes(q) || (w.premise || '').toLowerCase().includes(q),
  )
})

async function archiveWork(w) {
  if (!(await store.setWorkStatus(w.id, 'archived'))) showNotice(store.error || '归档失败', true)
}

async function unarchiveWork(w) {
  if (!(await store.setWorkStatus(w.id, 'active'))) showNotice(store.error || '恢复失败', true)
}

/** 全库搜索的结果点进来：同一篇里就直接换节，不然带着 node 跳过去 */
async function gotoSearchHit(hit) {
  if (!hit?.work_id) return
  if (store.work?.id === hit.work_id) {
    if (hit.node_id) await onSelectNode(hit.node_id)
    return
  }
  router.push({
    path: `/studio/${hit.work_id}`,
    query: hit.node_id ? { node: hit.node_id } : {},
  })
}

// 发布台账：只记「发出去之后才知道」的数，跟正文无关，所以放在作品弹层里
const pubDraft = ref({ published_at: '', url: '', reads: 0, likes: 0 })
const pubMap = computed(() => {
  const out = {}
  for (const p of store.pubSummary) out[p.work_id] = p
  return out
})

function pubOf(id) {
  return pubMap.value[id]
}

async function savePub() {
  const ok = await store.addPublication({
    published_at: pubDraft.value.published_at.trim(),
    url: pubDraft.value.url.trim(),
    reads: Number(pubDraft.value.reads) || 0,
    likes: Number(pubDraft.value.likes) || 0,
  })
  if (!ok) {
    showNotice(store.error || '保存发布记录失败', true)
    return
  }
  pubDraft.value = { published_at: '', url: '', reads: 0, likes: 0 }
  showNotice('发布记录已保存')
}

async function removePub(p) {
  if (!window.confirm(`删掉「${p.published_at || '未填日期'}」这条发布记录？`)) return
  await store.deletePublication(p.id)
}

/**
 * 当前作品那一档规模的层级信息，来自 /studio/content-types（content-types.yaml）。
 * 前端不再自己猜层级：单篇只有一层「节」，系列才有「讲」，小说才有「卷」。
 */
const scaleMeta = computed(() => {
  const k = kinds.value.find((x) => x.id === store.work?.kind)
  return k?.scales?.find((s) => s.id === store.work?.scale) || null
})
const leafLabel = computed(() => scaleMeta.value?.leaf_label || '节')
const containerLabel = computed(() => scaleMeta.value?.container_label || '')
const emptyHint = computed(() =>
  containerLabel.value
    ? `左侧点「+」先建${containerLabel.value}，再在里面建${leafLabel.value}。`
    : `这一档只有「${leafLabel.value}」一层：点左侧「+」建第一${leafLabel.value}，`
      + '或先点「大纲」按模板把目录落成节点。',
)

// ---- 导入旧稿 ----
// 当前选中的是容器（卷/篇）就挂进去，否则落在根上：跨卷移动还没做，落在根上最安全
const importOpen = ref(false)
const importParent = computed(() => {
  const n = store.node
  return n && n.ordinal == null ? { id: n.id, title: n.title } : { id: '', title: '' }
})
const importKind = computed(
  () => scaleMeta.value?.leaf_kind || (store.work?.kind === 'novel' ? 'chapter' : 'section'),
)

async function onImported(res) {
  showNotice(`已导入 ${res.created} 节`)
  if (res.firstId) await store.selectNode(res.firstId)
}

// ---- 队列（M4） ----
const queueHidden = ref(false)
const qc = computed(() => store.queue?.counts || {})
const showQueue = computed(() => {
  if (queueHidden.value) return false
  const q = store.queue
  if (!q) return false
  return store.queueActive || (q.counts?.done || 0) + (q.counts?.failed || 0) > 0
})
const queueTitle = computed(() => {
  const q = store.queue
  if (!q) return '队列'
  if (q.triage?.due) return '等待抽检'
  if (q.paused) return '队列已暂停'
  if (q.current) return `正在写：${q.current.title || q.current.task_type}`
  return `队列已排空 · 完成 ${q.counts?.done || 0} 节`
})

const showPipeline = computed(
  () => store.pipeline.running || store.pipeline.total > 0 || Boolean(store.pipeline.error),
)
const pipeTitle = computed(() => {
  const p = store.pipeline
  if (p.running) return p.title ? `正在生成：${p.title}` : '准备中…'
  return `整篇已生成 ${p.done}/${p.total} 节`
})
const pipePercent = computed(() => {
  const p = store.pipeline
  if (!p.total) return '0%'
  return Math.min(100, Math.round((p.done / p.total) * 100)) + '%'
})

function onKindChange() {
  applyScaleDefaults()
  form.value.subtype = subtypes.value[0]?.id || ''
}

function applyScaleDefaults() {
  form.value.scale = scales.value[0]?.id || 'single'
  const s = scales.value.find((x) => x.id === form.value.scale)
  if (s?.target_words) form.value.target_words = s.target_words
}

let saveTimer = 0

const saveLabel = computed(() => {
  if (!store.node) return ''
  // 失败要显眼：以前无论成功失败都写「已保存」，关页面前一秒才发现白写了
  if (saveState.value === 'error') return '保存失败'
  if (store.saving || saveState.value === 'saving') return '保存中…'
  if (saveState.value === 'dirty') return '未保存'
  return '已保存'
})

const wordCount = computed(() => {
  const text = contentDraft.value || ''
  const cjk = (text.match(/[\u4e00-\u9fff]/g) || []).length
  const words = text.replace(/[\u4e00-\u9fff]/g, ' ').split(/\s+/).filter(Boolean).length
  return cjk + words
})

/** 本节目标字数：只在大纲生成过的那一节有（存在 meta_json.outline 里） */
const nodeTarget = computed(() => {
  try {
    const meta = JSON.parse(store.work?.meta_json || '{}')
    return Number(meta?.outline?.[store.node?.id]?.target_words || 0)
  } catch {
    return 0
  }
})

// ---- 生成结果逐段采用 ----
const genParas = computed(() =>
  (store.genText || '')
    .split(/\n{2,}/)
    .map((s) => s.trim())
    .filter(Boolean),
)
const genPicked = ref([])

function isPicked(i) {
  return genPicked.value[i] !== false
}

const pickedCount = computed(() => genParas.value.filter((_, i) => isPicked(i)).length)

function pickAll(on) {
  genPicked.value = genParas.value.map(() => on)
}

// 流式生成时段落还在长，等停了再给勾选（默认全选）；重新生成时重置
watch(() => store.generating, (on, was) => {
  if (was && !on) pickAll(true)
})
watch(() => store.genText, (text) => {
  if (!text) genPicked.value = []
})

// ---- 写作日历（近 14 天，今天在最右）----
function ymd(dt) {
  const m = String(dt.getMonth() + 1).padStart(2, '0')
  const d = String(dt.getDate()).padStart(2, '0')
  return `${dt.getFullYear()}-${m}-${d}`
}

const last14 = computed(() => {
  const got = new Map((store.dailyWrites.days || []).map((d) => [d.day, d.chars]))
  const out = []
  const base = new Date()
  for (let i = 13; i >= 0; i--) {
    const dt = new Date(base.getFullYear(), base.getMonth(), base.getDate() - i)
    const day = ymd(dt)
    out.push({ day, chars: got.get(day) || 0 })
  }
  return out
})

const dailyMax = computed(() => Math.max(1, ...last14.value.map((d) => d.chars)))

function barHeight(chars) {
  if (!chars) return '3px'
  return Math.max(6, Math.round((chars / dailyMax.value) * 24)) + 'px'
}

const genStatus = computed(() => {
  if (store.generating) return store.genText ? '生成中' : '装配上下文…'
  if (store.genStopped) return '已停止'
  return '生成完成'
})

const genMetaText = computed(() => {
  // 服务端在 meta 事件里报了它实际用的模型，比下拉框选的值可信（「自动」时尤其如此）
  const used = store.genMeta?.model
  const n = store.genText.length
  if (!store.generating) return [used, `${n} 字`].filter(Boolean).join(' · ')
  // 实测约 140 字/秒，给用户一个可预期的剩余时间
  const remain = Math.max(1, Math.round((2800 - n) / 140))
  return [used, `${n} 字`, `约 ${remain}s`].filter(Boolean).join(' · ')
})

/** 下拉框的悬停说明：「自动」到底是什么意思，得写清楚 */
const modelHint = computed(() =>
  store.genModel
    ? `生成用 ${store.genModel}（大纲 / 整篇 / 排队 / 加工都跟着走）；该站点的账号要先在 /live 登录上`
    : '不指定模型，由后端落到它的默认主力模型；生成后看进度条上的实际模型',
)

function knownModel(id) {
  return store.allGenModels.some((m) => m.id === id)
}

function fmt(n) {
  if (!n) return '0'
  return n >= 10000 ? (n / 10000).toFixed(1) + '万' : String(n)
}

/** 把告警定位到正文：按证据原文找，找不到就退到前 24 / 前 12 字。 */
function locateEvidence(text) {
  const el = editorEl.value
  const full = contentDraft.value || ''
  if (!el || !text || !full) return false
  const needles = [text, text.slice(0, 24), text.slice(0, 12)].filter(Boolean)
  for (const needle of needles) {
    const at = full.indexOf(needle)
    if (at < 0) continue
    el.focus()
    el.setSelectionRange(at, at + needle.length)
    // 浏览器会把光标滚到可视区，但常常只差一点点，手工再校准一次
    const ratio = at / Math.max(full.length, 1)
    el.scrollTop = Math.max(0, ratio * el.scrollHeight - el.clientHeight / 3)
    return true
  }
  return false
}

function kindLabel(w) {
  if (w.kind === 'novel') return '长篇'
  return w.scale === 'series' ? '系列' : (w.subtype || '单篇')
}

onMounted(async () => {
  // 模型目录拉不到只影响下拉框选项，不能把整个写作台卡住，所以单独吞掉错误
  await Promise.all([
    store.fetchWorks(),
    store.loadTemplates('article'),
    store.loadContentTypes(),
    store.loadGenModels().catch(() => {}),
    // 书架上的「已发 · 阅读」来自台账；拉不到就不显示那一行，不影响写作
    store.loadPublicationSummary().catch(() => {}),
    store.loadIdeas().catch(() => {}),
    store.loadDailyWrites().catch(() => {}),
  ])
  if (!kinds.value.some((k) => k.id === form.value.kind)) form.value.kind = kinds.value[0]?.id || 'article'
  applyScaleDefaults()
  const id = route.params.id
  if (id) await openWork(id)
})

watch(() => route.params.id, (id) => {
  if (id && id !== store.activeWorkId) openWork(id)
})

watch(() => store.work?.id, () => { workEditOpen.value = false })

watch(() => store.node?.id, () => {
  titleDraft.value = store.node?.title || ''
  contentDraft.value = store.node?.content || ''
  saveState.value = 'saved'
})

// 键盘可达：Ctrl+S 立即存，Ctrl+Enter 生成（见 design-system MASTER.md）
function onKeydown(e) {
  if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'k') {
    e.preventDefault()
    openSearch()
    return
  }
  if (!(e.ctrlKey || e.metaKey)) return
  if (e.key === 's') {
    e.preventDefault()
    flushSave()
  } else if (e.key === 'Enter') {
    e.preventDefault()
    runGenerate()
  }
}
/**
 * 离开前的两条保险：
 * 1. 还没落盘就关标签页/刷新，让浏览器拦一下（saveState 不是 saved 时）；
 * 2. 切到别的标签页先存一次——比 beforeunload 更早，用户往往触发不到前者。
 */
function onBeforeUnload(e) {
  if (saveState.value === 'saved') return
  e.preventDefault()
  e.returnValue = ''
}

function onVisibilityChange() {
  if (document.visibilityState === 'hidden') flushSave()
}

onMounted(() => {
  window.addEventListener('keydown', onKeydown)
  window.addEventListener('beforeunload', onBeforeUnload)
  document.addEventListener('visibilitychange', onVisibilityChange)
})
onUnmounted(() => {
  window.removeEventListener('keydown', onKeydown)
  window.removeEventListener('beforeunload', onBeforeUnload)
  document.removeEventListener('visibilitychange', onVisibilityChange)
  stopQueuePoll()
  clearTimeout(noticeTimer)
})

/**
 * 换节前先落盘：上一节保存失败还直接切走，编辑器里那段字就再也没机会回到库里。
 * 能存上就静默通过，存不上才让人自己决定。
 */
async function onSelectNode(id) {
  if (!id || id === store.node?.id) return
  if (saveState.value !== 'saved') {
    const ok = await flushSave()
    if (!ok && !window.confirm('上一节还没存上，切走会丢掉那段改动。仍要切换吗？')) return
  }
  await store.selectNode(id)
}

async function openWork(id) {
  await store.selectWork(id)
  await store.loadTemplates(store.work?.kind || 'article')
  if (route.params.id !== id) router.replace(`/studio/${id}`)
  // 队列可能在关页面期间一直在跑，回来先把状态拉齐
  queueHidden.value = false
  await store.loadQueue()
  if (store.queueActive) startQueuePoll()
  // 默认选中第一个有正文的节点，省一次点击；从全库搜索跳进来时先认 ?node=
  const wanted = typeof route.query.node === 'string' ? route.query.node : ''
  const first = (wanted && store.tree.find((n) => n.id === wanted))
    || store.tree.find((n) => n.ordinal != null)
  if (first) await store.selectNode(first.id)
}

async function backToShelf() {
  // 回书架等于离开这一节：没存上的改动先重试，别一声不响地丢
  const ok = await flushSave()
  if (!ok && !window.confirm('这一节还没存上，回书架会丢掉这段改动。仍要离开吗？')) return
  // 刚写完的字要立刻反映到书架上的「今天字数 / 连续天」
  store.loadDailyWrites().catch(() => {})
  router.replace('/studio')
  store.work = null
  store.tree = []
  store.node = null
}

// ---- 操作结果的小提示（上下移到边、删除被备份拦住）----
const notice = ref('')
const noticeBad = ref(false)
let noticeTimer = 0

function showNotice(text, bad = false) {
  notice.value = text
  noticeBad.value = bad
  clearTimeout(noticeTimer)
  noticeTimer = setTimeout(() => (notice.value = ''), bad ? 5200 : 2600)
}

async function removeWork(w) {
  if (!window.confirm(
    `删除《${w.title}》？会连同它的节点树、正文与全部历史版本一并删掉。\n\n`
    + '删之前会先拷一份整库副本到 storage/backups/works/，要找回只能停服务把那个文件盖回去。')) return
  if (!(await store.deleteWork(w.id))) showNotice(store.error || '删除失败', true)
}

async function create() {
  creating.value = true
  store.error = ''
  try {
    const w = await store.createWork({ ...form.value })
    // 从灵感箱开的稿：记下指向，之后在库里能回看这句想法变成了哪篇
    if (pendingIdea.value) {
      await store.useIdea(pendingIdea.value, w.id)
      pendingIdea.value = ''
    }
    showCreate.value = false
    form.value = { kind: 'article', scale: 'single', subtype: 'tech', title: '', premise: '', target_words: 5000 }
    router.replace(`/studio/${w.id}`)
  } catch (e) {
    store.error = String(e?.message || e).slice(0, 200)
  } finally {
    creating.value = false
  }
}

async function quickCreate(parent) {
  const depth = parent ? parent.depth + 1 : 0
  // 层级跟着配置走：单篇没有容器层，根上的「+」就该直接建节。
  // 照小说的话术去建「卷」，只会让人以为单篇也得先搭一个壳。
  const meta = scaleMeta.value
  const container = depth === 0 && meta?.container_kind ? meta.container_kind : ''
  const kind = container || meta?.leaf_kind || (store.work.kind === 'novel' ? 'chapter' : 'section')
  const label = (container ? meta.container_label : meta?.leaf_label) || '节点'
  const title = container
    ? `第 ${store.tree.filter((n) => n.depth === 0).length + 1} ${label}`
    : `新${label}`
  const n = await store.createNode({ kind, title, parent_id: parent?.id || null })
  if (n) titleDraft.value = n.title
}

async function removeNode(node) {
  if (!window.confirm(
    `删除「${node.title}」及其子节点？会连同子树与全部历史版本删除。\n\n`
    + '正文已自动备份，可用 scripts/restore_backup.py 找回。')) return
  if (!(await store.deleteNode(node.id))) showNotice(store.error || '删除失败', true)
}

/**
 * 上下移一节：与紧邻的前/后兄弟互换位置。
 *
 * 先 flushSave：交换会把两节的章号连同图谱上的章号引用一起对调，编辑器里那份
 * 还没落盘的正文不先存上，刷出来的树会与库里对不上。
 * 移完重跑一次一致性检查——那套规则按章号判定（伏笔超期、死后还出场），
 * 顺序一变结论就可能变，纯本地比对不调模型。告警里的章号文本要等下一次检查才刷新。
 */
async function moveNode(node, direction) {
  await flushSave()
  const r = await store.moveNode(node.id, direction)
  if (!r.moved) {
    showNotice(r.reason || '移动失败')
    return
  }
  showNotice(`已与「${r.swapped_with_title}」交换位置`)
  await store.runCheck()
  await panelRef.value?.refreshContext()
}

function onInput() {
  saveState.value = 'dirty'
  clearTimeout(saveTimer)
  saveTimer = setTimeout(flushSave, 800)
}

/** 立即落盘；已经是最新状态就不重复请求。返回是否真的存上 */
async function flushSave() {
  if (!store.node) return false
  clearTimeout(saveTimer)
  if (saveState.value === 'saved') return true
  const wasError = saveState.value === 'error'
  saveState.value = 'saving'
  const ok = await store.saveNode({ content: contentDraft.value })
  if (!ok) {
    saveState.value = 'error'
    // 自动保存每 800ms 会再试一次；服务端挂着的时候不该每敲一个字弹一遍
    if (!wasError) showNotice(store.error || '保存失败：正文还在页面上，先别关', true)
    return false
  }
  saveState.value = 'saved'
  return true
}

async function saveTitle() {
  if (!titleDraft.value.trim()) return
  if (!(await store.saveNode({ title: titleDraft.value.trim() }))) {
    showNotice(store.error || '标题没存上，再试一次', true)
  }
}

async function changeStatus(e) {
  if (!(await store.saveNode({ status: e.target.value }, { snapshot: true }))) {
    showNotice(store.error || '状态没存上，再试一次', true)
  }
}

function toggleWorkEdit() {
  workEditOpen.value = !workEditOpen.value
  if (workEditOpen.value) {
    workTitleDraft.value = store.work?.title || ''
    workPremiseDraft.value = store.work?.premise || ''
    workDigestDraft.value = store.work?.digest || ''
    store.loadPublications()
  }
}

async function saveWorkMeta() {
  const title = workTitleDraft.value.trim()
  if (!title || !store.work) return
  savingWork.value = true
  const ok = await store.updateWork({
    title,
    premise: workPremiseDraft.value.trim(),
    digest: workDigestDraft.value.trim(),
  })
  savingWork.value = false
  if (!ok) {
    showNotice(store.error || '保存失败', true)
    return
  }
  workEditOpen.value = false
  showNotice('作品信息已更新')
}

/** 弹层里的「AI 写摘要」：写完先填进输入框，确认后再随「保存」落库 */
async function makeDigestInPop() {
  if (digestBusy.value) return
  digestBusy.value = true
  try {
    const got = await store.writeDigest()
    if (got) {
      workDigestDraft.value = got
      showNotice('摘要写好了，确认后点保存')
    } else {
      showNotice(store.error || '写摘要失败', true)
    }
  } finally {
    digestBusy.value = false
  }
}

/**
 * 发布检查里点「定位」：先切到那一节，再选中证据片段。
 * 切节点会重载编辑器，没落盘的字不先存就会丢。
 */
async function gotoIssue(item) {
  await flushSave()
  if (item?.node_id && item.node_id !== store.node?.id) {
    await store.selectNode(item.node_id)
  }
  await nextTick()
  if (item?.evidence) locateEvidence(item.evidence)
}

// 提示词框最多长到几行，再往上就自己滚；CSS 里 .gen-input 的 max-height 要和它对齐
const INSTR_MAX_ROWS = 6

/**
 * 提示词框按内容长高。先复位成 auto 再量 scrollHeight，
 * 不然删字之后高度只会停在最高处，回不去。
 */
function autosizeInstruction() {
  const el = instrEl.value
  if (!el) return
  el.style.height = 'auto'
  const line = parseFloat(getComputedStyle(el).lineHeight) || 21
  const max = Math.round(line * INSTR_MAX_ROWS + 16)
  el.style.height = Math.min(el.scrollHeight, max) + 'px'
}

watch(instruction, () => nextTick(autosizeInstruction))

/**
 * 回车生成、Shift+回车换行。
 * 输入法选词时那个回车不能当提交：打中文时按一下就把整节跑掉，很难受。
 */
function onInstructionKeydown(e) {
  if (e.key !== 'Enter' || e.shiftKey || e.ctrlKey || e.metaKey) return
  if (e.isComposing) return
  e.preventDefault()
  runGenerate()
}

async function runGenerate() {
  if (store.generating || !store.node) return
  if (taskType.value === 'polish' && !contentDraft.value.trim()) {
    // 下拉框已经禁了「润色」，这里是快捷键那条路兜底
    showNotice('润色要有正文：这一节还是空的，先写正文或切到「写本节」', true)
    return
  }
  // 有未保存改动先落盘，否则装配用的是旧正文
  if (!(await flushSave())) {
    // 装配读的是库里的正文：没存上就生成，模型看到的是上一版，白跑一次
    showNotice('正文没存上，先在顶部「保存失败」处重试，再生成', true)
    return
  }
  await store.generate({ task_type: taskType.value, instruction: instruction.value })
}

async function adopt() {
  const append = taskType.value === 'continue'
  // 版本来源按任务标：右栏「历史」里 AI 润色与 AI 草稿要能分开看
  const source = taskType.value === 'polish' ? 'ai_polish' : 'ai_draft'
  const total = genParas.value.length
  const paras = genParas.value.filter((_, i) => isPicked(i))
  if (!paras.length) {
    showNotice('一段都没勾：勾上要用的段落再采用', true)
    return
  }
  const ok = await store.adoptGenerated({ append, source, text: paras.join('\n\n') })
  if (!ok) {
    // 预览里那份还在，稍后再点一次就行；不能顺手把编辑器刷成旧正文
    showNotice(store.error || '采用失败：生成结果还在预览里', true)
    return
  }
  contentDraft.value = store.node?.content || ''
  if (paras.length < total) showNotice(`已采用 ${paras.length}/${total} 段`)
}

/**
 * 回滚到某个历史版本。先把编辑器里那份 flushSave 掉，再让服务端写回——
 * 顺序反了的话，服务端看到的是上一版自动保存的正文，手里这份改动就直接丢了。
 */
async function onRestoreVersion(versionId) {
  // 回滚会盖掉编辑器里那一份：没存上就别动，不然两边一起丢
  if (!(await flushSave())) {
    showNotice('当前正文没存上，先重试保存再回滚', true)
    return
  }
  const ok = await store.restoreVersion(versionId)
  if (!ok) return
  contentDraft.value = store.node?.content || ''
  saveState.value = 'saved'
  // 正文换了，上下文预览（含摘要过期提示）得跟着重装配一次，纯本地不调模型
  await panelRef.value?.refreshContext()
}

// ==================== 大纲 / 整篇 / 加工 ====================

async function doOutline() {
  if (!store.work) return
  const hasNodes = store.tree.some((n) => n.ordinal != null)
  const append = hasNodes
    ? window.confirm('已有内容节点。确认追加一套新目录吗？（取消则中止）')
    : false
  if (hasNodes && !append) return
  // 提示词框那句跑完大纲不清空：它同时是逐节生成要用的那一句，
  // 一清就是把用户刚写的要求抹掉，接着生成还得重打一遍
  await store.generateOutline({ instruction: instruction.value, append })
}

async function doPipeline() {
  if (store.pipeline.running || !store.work) return
  await flushSave()
  resetPipeline()
  await store.runPipeline({ instruction: instruction.value })
}

// ---- 队列（M4） ----
let queueTimer = 0

function startQueuePoll() {
  if (queueTimer) return
  queueTimer = setInterval(async () => {
    await store.loadQueue()
    if (!store.queueActive) stopQueuePoll()
    // 单节写完后正文被换掉了，没选中的情况下也刷新一下当前节点
    if (store.queue?.current == null && store.node) {
      const r = await fetchNodeQuietly(store.node.id)
      if (r && r.content !== contentDraft.value) applyRemoteContent(r)
    }
  }, 3000)
}

function stopQueuePoll() {
  if (queueTimer) clearInterval(queueTimer)
  queueTimer = 0
}

async function fetchNodeQuietly(id) {
  try {
    const r = await apiFetch(`/studio/nodes/${id}`)
    return r.ok ? await r.json() : null
  } catch {
    return null
  }
}

/** 队列写完后把正文换成服务端版本（只在本地没有未保存修改时） */
function applyRemoteContent(node) {
  if (saveState.value === 'dirty' || store.saving) return
  contentDraft.value = node.content || ''
  store.node = node
  saveState.value = 'saved'
}

async function doEnqueue() {
  if (!store.work) return
  await flushSave()
  const j = await store.enqueueBatch({ instruction: instruction.value })
  if (!j) return
  queueHidden.value = false
  startQueuePoll()
  if (store.node) await store.selectNode(store.node.id)
}

async function pauseQ() {
  await store.pauseQueue('手动暂停')
}

async function resumeQ() {
  await store.resumeQueue()
  startQueuePoll()
}

async function doTriage(verdict) {
  await store.triageQueue(verdict)
  if (store.queueActive) startQueuePoll()
}

function resetPipeline() {
  store.pipeline.total = 0
  store.pipeline.done = 0
  store.pipeline.chars = 0
  store.pipeline.preview = ''
  store.pipeline.error = ''
  store.pipeline.title = ''
  store.pipeline.indexed = 0
  store.pipeline.summarized = 0
}

function openTransform() {
  if (!store.node) return
  const el = editorEl.value
  let sel = ''
  if (el && el.selectionStart !== el.selectionEnd) {
    sel = contentDraft.value.slice(el.selectionStart, el.selectionEnd)
  }
  selectedText.value = sel
  transformOpen.value = true
}

/** 采用加工结果：起标题回填标题，其余有选区替换选区、没选区替换整节 */
function onTransformAdopt(payload) {
  if (payload && typeof payload === 'object' && payload.action === 'title') {
    applyTitle(payload.text, payload.target)
    return
  }
  const text = typeof payload === 'string' ? payload : payload.text
  const el = editorEl.value
  if (el && el.selectionStart !== el.selectionEnd) {
    const s = el.selectionStart
    const e = el.selectionEnd
    contentDraft.value = contentDraft.value.slice(0, s) + text + contentDraft.value.slice(e)
  } else {
    contentDraft.value = text
  }
  saveState.value = 'dirty'
  flushSave()
}

/**
 * 起标题的落点两个：本节标题，或作品标题。
 * 单篇发公众号时真正当文章标题用的是作品标题（导出那句 `# 作品标题`），
 * 所以两个都得给，只回一个会逼人在两处之间手工搬字。
 */
async function applyTitle(text, target) {
  const clean = (text || '').replace(/[《》]/g, '').trim().slice(0, 120)
  if (!clean) return
  if (target === 'work') {
    if (await store.updateWork({ title: clean })) showNotice(`作品标题已改成「${clean}」`)
    else showNotice(store.error || '改作品标题失败', true)
    return
  }
  titleDraft.value = clean
  await store.saveNode({ title: clean })
  showNotice(`本节标题已改成「${clean}」`)
}
</script>

<style scoped>
.app-shell {
  display: flex;
  height: 100vh;
  height: 100dvh;
  /* 最后一行兜住 dvh：它在个别环境里会把视口算成一半，整块就只剩上半截、下面全空。
     100% 走的是 main.css 里 html/body/#app 那条高链，不依赖视口单位的实现。 */
  height: 100%;
  overflow: hidden;
  background: var(--bg);
}
.studio {
  flex: 1;
  min-width: 0;
  display: flex;
  background: var(--bg);
}

/* ---------- 书架 ---------- */
.shelf {
  flex: 1;
  overflow-y: auto;
  padding: 34px 40px 60px;
  max-width: 1080px;
  margin: 0 auto;
  width: 100%;
}
.shelf-head {
  display: flex;
  align-items: flex-start;
  gap: 16px;
  margin-bottom: 22px;
}
.shelf-head h1 {
  margin: 0 0 5px;
  font-family: theme('fontFamily.serif');
  font-size: 26px;
  font-weight: 600;
  letter-spacing: 0.01em;
  color: var(--text);
}
.shelf-sub {
  margin: 0;
  font-size: 12.5px;
  color: var(--text-faint);
}
.shelf-tools {
  margin-left: auto;
  display: flex;
  align-items: center;
  justify-content: flex-end;
  flex-wrap: wrap;
  gap: 8px;
}
.shelf-search {
  display: inline-flex;
  align-items: center;
  gap: 7px;
  height: 30px;
  padding: 0 9px;
  border: 1px solid var(--line);
  border-radius: var(--radius-control);
  background: var(--panel);
  color: var(--text-faint);
}
.shelf-search:focus-within {
  border-color: rgba(217, 164, 91, 0.45);
}
.shelf-search .field {
  width: 168px;
  height: 26px;
  padding: 0;
  border: none;
  background: transparent;
  font-size: 12px;
}
.shelf-search .field:focus {
  outline: none;
}
.shelf-views {
  display: flex;
  gap: 6px;
  margin: -6px 0 18px;
}
.view-chip {
  height: 26px;
  padding: 0 11px;
  border: 1px solid transparent;
  border-radius: 999px;
  background: transparent;
  color: var(--text-faint);
  font-size: 12px;
  font-family: theme('fontFamily.mono');
  cursor: pointer;
  transition: color 0.18s var(--ease), background-color 0.18s var(--ease);
}
.view-chip:hover {
  color: var(--text);
  background: rgba(255, 255, 255, 0.04);
}
.view-chip.is-on {
  color: var(--run);
  background: rgba(217, 164, 91, 0.1);
}
.write-log {
  display: flex;
  align-items: center;
  gap: 16px;
  margin: -6px 0 14px;
  padding: 8px 12px;
  border: 1px solid var(--line-soft);
  border-radius: var(--radius-control);
  background: var(--panel);
  font-size: 11.5px;
  color: var(--text-faint);
}
.wl-item strong {
  margin-right: 4px;
  font-family: theme('fontFamily.mono');
  font-size: 14px;
  color: var(--text);
}
.wl-days {
  margin-left: auto;
  display: flex;
  align-items: flex-end;
  gap: 3px;
  height: 26px;
}
.wl-bar {
  width: 4px;
  min-height: 3px;
  border-radius: 1px;
  background: rgba(255, 255, 255, 0.09);
}
.wl-bar.is-on {
  background: rgba(217, 164, 91, 0.65);
}
.ideas {
  margin-bottom: 18px;
  padding: 12px 14px;
  border: 1px dashed var(--line);
  border-radius: var(--radius-panel);
}
.ideas-head {
  display: flex;
  align-items: baseline;
  gap: 10px;
  margin-bottom: 8px;
}
.ideas-head h2 {
  margin: 0;
  font-size: 13px;
  font-weight: 600;
  color: var(--text);
}
.ideas-sub {
  font-size: 11px;
  color: var(--text-faint);
}
.ideas-add {
  display: flex;
  gap: 8px;
}
.ideas-add .field {
  flex: 1;
  min-width: 0;
}
.idea-list {
  list-style: none;
  margin: 8px 0 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 2px;
  max-height: 150px;
  overflow-y: auto;
}
.idea-row {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 5px 6px;
  border-radius: 5px;
}
.idea-row:hover {
  background: rgba(255, 255, 255, 0.035);
}
.idea-text {
  flex: 1;
  min-width: 0;
  font-size: 12px;
  color: var(--text-muted);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.idea-del {
  border: none;
  background: transparent;
  color: var(--text-faint);
  font-size: 11px;
  cursor: pointer;
}
.idea-del:hover {
  color: var(--danger);
}
.link-btn {
  margin-left: 4px;
  padding: 0;
  border: none;
  background: transparent;
  color: var(--run);
  font-size: inherit;
  font-family: inherit;
  text-decoration: underline;
  text-underline-offset: 2px;
  cursor: pointer;
}
.link-btn:hover {
  color: var(--text);
}
.create {
  padding: 16px 18px;
  margin-bottom: 22px;
  display: flex;
  flex-direction: column;
  gap: 12px;
}
.create-row {
  display: flex;
  gap: 10px;
  flex-wrap: wrap;
}
.fl {
  display: flex;
  flex-direction: column;
  gap: 5px;
}
.fl > span {
  font-size: 11px;
  color: var(--text-faint);
}
.fl .field {
  width: 132px;
}
.fl.grow {
  flex: 1;
  min-width: 200px;
}
.fl.grow .field,
.fl > .field.grow {
  width: 100%;
}
.fl.small .field {
  width: 110px;
}
.create-foot {
  display: flex;
  align-items: flex-end;
  gap: 12px;
}
.create-foot .btn {
  margin-left: auto;
}
.err {
  margin: 0;
  font-size: 12px;
  color: var(--danger);
}
.grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(240px, 1fr));
  gap: 12px;
}
.work-card {
  position: relative;
  display: flex;
  flex-direction: column;
  gap: 5px;
  padding: 15px 16px;
  border: 1px solid var(--line);
  border-radius: var(--radius-panel);
  background: var(--panel);
  text-align: left;
  cursor: pointer;
  transition: border-color 0.18s var(--ease), background-color 0.18s var(--ease);
}
.work-del {
  position: absolute;
  top: 8px;
  right: 8px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 22px;
  height: 22px;
  border: none;
  border-radius: 5px;
  background: transparent;
  color: var(--text-faint);
  cursor: pointer;
  opacity: 0;
  transition: opacity 0.15s var(--ease), color 0.15s var(--ease);
}
.work-arch {
  position: absolute;
  top: 8px;
  right: 34px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 22px;
  height: 22px;
  border: none;
  border-radius: 5px;
  background: transparent;
  color: var(--text-faint);
  cursor: pointer;
  opacity: 0;
  transition: opacity 0.15s var(--ease), color 0.15s var(--ease);
}
.work-card:hover .work-arch,
.work-card:focus-within .work-arch {
  opacity: 1;
}
.work-arch:hover {
  color: var(--run);
  background: rgba(217, 164, 91, 0.12);
}
.work-card:hover .work-del,
.work-card:focus-within .work-del {
  opacity: 1;
}
.work-del:hover {
  color: var(--danger);
  background: rgba(228, 120, 120, 0.1);
}
.work-card:hover {
  border-color: rgba(217, 164, 91, 0.45);
  background: var(--panel-2);
}
.work-kind {
  font-family: theme('fontFamily.mono');
  font-size: 10px;
  letter-spacing: 0.06em;
  text-transform: uppercase;
  color: var(--run);
}
.work-title {
  font-family: theme('fontFamily.serif');
  font-size: 16px;
  font-weight: 600;
  color: var(--text);
}
.work-meta {
  font-size: 11px;
  color: var(--text-faint);
  font-family: theme('fontFamily.mono');
}
.work-pub {
  font-size: 11px;
  font-family: theme('fontFamily.mono');
  color: var(--run);
}
/* ---------- 发布记录（作品弹层内） ---------- */
.pub-box {
  border-top: 1px solid var(--line-soft);
  padding-top: 8px;
}
.pub-box summary {
  font-size: 11px;
  color: var(--text-faint);
  cursor: pointer;
}
.pub-row {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 4px 0;
  font-size: 11px;
  color: var(--text-muted);
}
.pub-date {
  font-family: theme('fontFamily.mono');
  color: var(--text-faint);
}
.pub-num {
  flex: 1;
  min-width: 0;
}
.pub-link {
  color: var(--run);
  text-decoration: none;
}
.pub-link:hover {
  text-decoration: underline;
}
.pub-del {
  border: none;
  background: transparent;
  font-size: 11px;
  color: var(--text-faint);
  cursor: pointer;
}
.pub-del:hover {
  color: var(--danger);
}
.pub-form {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 6px;
  margin-top: 6px;
}
.pub-form .btn {
  grid-column: 1 / -1;
  height: 26px;
  font-size: 11px;
}
.work-premise {
  font-size: 12px;
  line-height: 1.6;
  color: var(--text-muted);
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
  overflow: hidden;
}
.shelf-empty {
  margin-top: 30px;
  font-size: 13px;
  color: var(--text-faint);
}

/* ---------- 编辑器 ---------- */
.editor {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
  background: var(--bg);
}
.ed-top {
  display: flex;
  align-items: center;
  gap: 9px;
  padding: 9px 16px;
  border-bottom: 1px solid var(--line-soft);
  background: rgba(17, 19, 24, 0.7);
}
.ed-work-wrap {
  position: relative;
  display: flex;
  min-width: 0;
}
.ed-work {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  max-width: 240px;
  padding: 3px 6px;
  border: 1px solid transparent;
  border-radius: 6px;
  background: none;
  font: inherit;
  font-size: 12.5px;
  color: var(--text-muted);
  cursor: pointer;
  transition: color 0.18s var(--ease), border-color 0.18s var(--ease),
    background-color 0.18s var(--ease);
}
.ed-work-name {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.ed-work:hover,
.ed-work:focus {
  color: var(--text);
  border-color: var(--line);
  background: var(--panel-2);
  outline: none;
}
/* 作品信息就地编辑：贴着标题往下展开，不弹到屏幕中间打断写作 */
.work-pop {
  position: absolute;
  top: calc(100% + 6px);
  left: 0;
  z-index: 60;
  width: 340px;
  display: flex;
  flex-direction: column;
  gap: 10px;
  padding: 12px 14px;
  border: 1px solid var(--line);
  border-radius: var(--radius-panel);
  background: var(--panel);
  box-shadow: 0 18px 44px -20px rgba(0, 0, 0, 0.8);
}
.work-pop .fl {
  display: flex;
  flex-direction: column;
  gap: 5px;
}
.work-pop .fl > span {
  font-size: 11px;
  color: var(--text-faint);
}
.work-pop .field {
  width: 100%;
}
.work-pop textarea {
  resize: vertical;
  line-height: 1.7;
}
.work-pop-tip {
  margin: 0;
  font-size: 11px;
  color: var(--text-faint);
  line-height: 1.6;
}
.work-pop-row {
  display: flex;
  align-items: center;
  gap: 8px;
}
.work-pop-row .btn {
  height: 24px;
  padding: 0 8px;
  font-size: 11px;
}
.work-pop-tip.inline {
  margin-left: auto;
  font-family: theme('fontFamily.mono');
}
.work-pop-act {
  display: flex;
  justify-content: flex-end;
  gap: 8px;
}
.ed-save {
  font-size: 11px;
  color: var(--text-faint);
  font-family: theme('fontFamily.mono');
}
/* 存不上要能看见：变红，并且点一下直接重试 */
.ed-save.is-bad {
  color: var(--danger);
  cursor: pointer;
  text-decoration: underline dotted;
}
.ed-right {
  margin-left: auto;
  display: flex;
  align-items: center;
  gap: 6px;
}
.ed-right .btn {
  height: 26px;
  padding: 0 9px;
  font-size: 11.5px;
  gap: 4px;
}
/* 模型选择：沿用原来的胶囊观感，但它现在是真的能改的 */
.ed-model {
  font-family: theme('fontFamily.mono');
  font-size: 10.5px;
  color: var(--text-faint);
  padding: 3px 19px 3px 8px;
  height: 26px;
  border: 1px solid var(--line-soft);
  border-radius: 999px;
  background-color: var(--panel-2);
  /* 与 main.css 里 select.field 同一枚箭头，只是更小 */
  background-image: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='10' height='10' viewBox='0 0 24 24' fill='none' stroke='%23A3B0C2' stroke-width='2.6' stroke-linecap='round' stroke-linejoin='round'%3E%3Cpath d='m6 9 6 6 6-6'/%3E%3C/svg%3E");
  background-repeat: no-repeat;
  background-position: right 7px center;
  appearance: none;
  max-width: 186px;
  cursor: pointer;
  transition: color 0.18s var(--ease), border-color 0.18s var(--ease);
}
.ed-model:hover,
.ed-model:focus {
  color: var(--text-muted);
  border-color: var(--line);
  outline: none;
}
/* 弹层由系统画，不显式给背景的话在深色主题下会白底白字 */
.ed-model option,
.ed-model optgroup {
  background: var(--panel-2);
  color: var(--text);
  font-family: theme('fontFamily.sans');
}
.ed-empty {
  flex: 1;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 6px;
  color: var(--text-faint);
}
.ed-empty h2 {
  margin: 4px 0 0;
  font-size: 15px;
  font-weight: 500;
  color: var(--text-muted);
}
.ed-empty p {
  margin: 0;
  font-size: 12.5px;
}
.ed-head {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 18px 0 8px;
  /* 与正文同宽：38em @ 17px */
  width: min(646px, calc(100% - 64px));
  margin: 0 auto;
}
.ed-title {
  flex: 1;
  min-width: 0;
  border: none;
  background: transparent;
  font-family: theme('fontFamily.serif');
  font-size: 24px;
  font-weight: 600;
  color: var(--text);
  padding: 2px 0;
}
.ed-title:focus {
  outline: none;
  border-bottom: 1px solid var(--line);
}
.status-select {
  width: 84px;
  height: 28px;
  font-size: 12px;
}
.ed-count {
  font-family: theme('fontFamily.mono');
  font-size: 11px;
  color: var(--text-faint);
  white-space: nowrap;
}
.ed-count.over {
  color: var(--warn);
}
.ed-prog {
  width: 46px;
  height: 3px;
  border-radius: 2px;
  background: rgba(255, 255, 255, 0.08);
  overflow: hidden;
}
.ed-prog i {
  display: block;
  height: 100%;
  background: var(--run);
}
.ed-body {
  flex: 1;
  min-height: 0;
  width: 100%;
  display: flex;
  justify-content: center;
  padding: 0 32px;
}
.ed-textarea {
  width: min(646px, 100%);
  height: 100%;
  resize: none;
  border: none;
  background: transparent;
  color: var(--text);
  font-family: theme('fontFamily.serif');
  font-size: 17px;
  line-height: 1.9;
  letter-spacing: 0.01em;
  padding: 8px 0 30px;
  outline: none;
}
.ed-textarea::placeholder {
  color: var(--text-faint);
  font-family: theme('fontFamily.sans');
  font-size: 13.5px;
  line-height: 1.7;
}

/* ---------- 操作结果提示（浮在内容外角，不插进布局，不然行高会跳） ---------- */
.notice {
  position: fixed;
  left: 16px;
  bottom: 16px;
  z-index: 90;
  max-width: 320px;
  padding: 7px 11px;
  border: 1px solid var(--line);
  border-radius: 8px;
  background: var(--panel-2);
  color: var(--text-muted);
  font-size: 11.5px;
  line-height: 1.6;
  box-shadow: 0 8px 24px rgba(0, 0, 0, 0.32);
}
/* 删除被备份拦住不是「没什么发生」，得看得出一条没删成 */
.notice--bad {
  border-color: rgba(217, 107, 107, 0.45);
  color: var(--danger);
}

/* ---------- 生成预览 ---------- */
.gen {
  margin: 0 32px;
  border: 1px solid rgba(217, 164, 91, 0.28);
  border-radius: var(--radius-panel);
  background: rgba(217, 164, 91, 0.045);
  padding: 10px 14px 12px;
  max-height: 34vh;
  display: flex;
  flex-direction: column;
}
.gen-head {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 11.5px;
  color: var(--text-muted);
}
.gen-title {
  display: inline-flex;
  align-items: center;
  gap: 6px;
}
.gen-meta {
  margin-left: auto;
  font-family: theme('fontFamily.mono');
  font-size: 10.5px;
  color: var(--text-faint);
}
.spinner {
  width: 10px;
  height: 10px;
  border: 1.6px solid rgba(217, 164, 91, 0.3);
  border-top-color: var(--run);
  border-radius: 50%;
  animation: spin 0.8s linear infinite;
}
@keyframes spin {
  to {
    transform: rotate(360deg);
  }
}
.gen-text {
  margin: 8px 0 0;
  overflow-y: auto;
  font-family: theme('fontFamily.serif');
  font-size: 13.5px;
  line-height: 1.8;
  color: var(--text);
  white-space: pre-wrap;
  word-break: break-word;
}
.gen-para {
  display: flex;
  align-items: flex-start;
  gap: 8px;
  padding: 2px 4px;
  border-radius: 4px;
  cursor: pointer;
}
.gen-para:hover {
  background: rgba(255, 255, 255, 0.04);
}
.gen-para-box {
  margin-top: 8px;
  flex-shrink: 0;
  accent-color: var(--run);
}
.gen-para-text {
  flex: 1;
  min-width: 0;
  white-space: pre-wrap;
}
.gen-para.is-off .gen-para-text {
  opacity: 0.38;
  text-decoration: line-through;
}
.gen-err {
  margin: 8px 0 0;
  font-size: 12px;
  color: var(--danger);
}
.gen-stop {
  margin: 8px 0 0;
  font-size: 11.5px;
  color: var(--text-muted);
}
.gen-actions {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-top: 9px;
}
.gen-tip {
  font-size: 11px;
  color: var(--text-faint);
}
.gen-warn {
  margin: 6px 0 0;
  font-size: 11.5px;
  line-height: 1.6;
  color: var(--warn);
}
.gen-src {
  margin: 4px 0 0;
  font-size: 11px;
  color: var(--text-faint);
}

/* ---------- 整篇流水线进度 ---------- */
.pipe {
  margin: 0 32px 8px;
  padding: 9px 12px 10px;
  border: 1px solid var(--line);
  border-radius: var(--radius-panel);
  background: var(--panel);
  display: flex;
  flex-direction: column;
  gap: 7px;
}
.pipe-head {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 11.5px;
  color: var(--text-muted);
}
.pipe-title {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  color: var(--text);
}
.pipe-meta {
  margin-left: auto;
  font-family: theme('fontFamily.mono');
  font-size: 10.5px;
  color: var(--text-faint);
}
.pipe .bar {
  height: 4px;
  border-radius: 2px;
  background: var(--line-soft);
  overflow: hidden;
}
.pipe .bar-fill {
  height: 100%;
  border-radius: 2px;
  background: var(--run);
  opacity: 0.8;
  transition: width 0.3s var(--ease);
}
.pipe-preview {
  margin: 0;
  max-height: 14vh;
  overflow-y: auto;
  font-family: theme('fontFamily.serif');
  font-size: 12.5px;
  line-height: 1.75;
  color: var(--text-muted);
  white-space: pre-wrap;
  word-break: break-word;
}
.pipe-err {
  margin: 0;
  font-size: 11.5px;
  color: var(--danger);
}

/* 队列：不确定进度用一条缓慢来回的光带，比假百分比诚实 */
.queue {
  border-color: rgba(217, 164, 91, 0.28);
}
.pipe .bar-fill.indeterminate {
  width: 38%;
  background: linear-gradient(90deg, transparent, var(--run), transparent);
  animation: queue-sweep 1.6s linear infinite;
}
@keyframes queue-sweep {
  from {
    margin-left: -38%;
  }
  to {
    margin-left: 100%;
  }
}
.triage {
  display: flex;
  flex-direction: column;
  gap: 7px;
  padding: 9px 10px;
  border: 1px solid rgba(217, 164, 91, 0.35);
  border-radius: var(--radius-control);
  background: rgba(217, 164, 91, 0.05);
}
.triage-title {
  margin: 0;
  font-size: 11.5px;
  line-height: 1.6;
  color: var(--text-muted);
}
.triage-list {
  display: flex;
  flex-direction: column;
  gap: 3px;
}
.triage-item {
  display: flex;
  align-items: baseline;
  gap: 8px;
  font-size: 12px;
  color: var(--text);
}
.triage-score {
  margin-left: auto;
  font-family: theme('fontFamily.mono');
  font-size: 11px;
  color: var(--run);
}
.triage-act {
  display: flex;
  gap: 6px;
}
.mini {
  border: 1px solid var(--line);
  border-radius: 6px;
  background: transparent;
  color: var(--text-faint);
  font-size: 11px;
  height: 22px;
  padding: 0 7px;
  cursor: pointer;
}
.mini:hover {
  color: var(--text);
  border-color: var(--line-strong);
}

/* ---------- 生成工具条 ---------- */
.gen-bar {
  display: flex;
  /* 提示词框会长高，按钮贴底对齐，不跟着上下跳 */
  align-items: flex-end;
  gap: 8px;
  padding: 10px 32px 14px;
  border-top: 1px solid var(--line-soft);
  background: rgba(17, 19, 24, 0.8);
}
.task-select {
  width: 108px;
  flex-shrink: 0;
}
/* 两行起步，写到 6 行封顶后自己滚（上限与 script 里的 INSTR_MAX_ROWS 对齐） */
.gen-input {
  flex: 1;
  min-width: 0;
  min-height: 56px;
  max-height: 140px;
  overflow-y: auto;
}
.gen-bar .btn {
  flex-shrink: 0;
}

@media (max-width: 1200px) {
  .shelf {
    padding: 24px 20px 50px;
  }
  .ed-head,
  .ed-textarea {
    width: 100%;
  }
  .gen-bar {
    padding: 10px 16px 14px;
  }
  .gen {
    margin: 0 16px;
  }
  .pipe {
    margin: 0 16px 8px;
  }
}

/* 动效尊重系统偏好 */
@media (prefers-reduced-motion: reduce) {
  .spinner {
    animation-duration: 3s;
  }
  .work-card,
  .ed-textarea {
    transition: none;
  }
}
</style>
