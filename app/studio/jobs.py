"""任务队列：可暂停、可断点续跑；默认串行，直连通道下的章级拆解可并行。

为什么默认还是串行：

上游是浏览器反代时，并发只会互相打断页面状态，还会把风控风险成倍放大。
串行跑的代价是慢，但慢是可预期的；并发跑出来的是一堆随机失败，没法排查。

什么时候允许并行（判据在 `_parallel_slots`）：

只有「直连 API 通道 + 章级拆解」这一种组合开并发：它没有风控也没有页面状态，
而且每章各写自己的卡片、彼此不共享中间状态。写正文永远串行——第 5 章要看到
第 4 章的摘要，并行跑出来的是几份互不认识的稿子，省了时间坏了质量。

三条必须做对的事：

1. **断点**：进程重启后把 `running` 退回 `queued`。不这么做，跑到一半崩溃时
   那些 job 既不会再被领，也不会重试，永远卡着。
2. **可重试 vs 不可重试**：超时、网络抖动值得退避重试；登录失效、滑块、账号受限
   重试一百次也没用，必须整队停下来等人——而且不能消耗这条任务的重试次数。
3. **抽检卡点**：连续跑完 N 节就停下来让人看两眼。连续 50 章废稿的代价是
   几小时加一次风控，而抽检只花几分钟。
"""
from __future__ import annotations

import asyncio
import time
from typing import Any, Optional

from loguru import logger

from app.llm.gateway import pick_model
from app.studio import content_types, tasks

# 每类任务的粗估耗时（秒），给 ETA 用。
# draft 按实测值给（含装配与后处理，900 字/章约 28s）；调大只会让 ETA 更保守。
ETA_PER_TASK = {
    "draft": 30.0,
    "beats": 7.0,
    "polish": 20.0,
    # deai 与 polish 同档：一次模型调用 + 索引/摘要/检查/打分全套后处理。
    # 不给它估个准确值，整本去味时进度条会一直跳得很乐观。
    "deai": 20.0,
    "check": 2.0,
    "summarize": 8.0,
    "extract": 8.0,
    # 拆解按章输入 1.2 万字上限、输出几百字卡片；反代通道还有 30s 最小间隔，
    # 一次 40-60 秒是常态。给 45 是让队列 ETA 不骗人（百万字的书几百次调用，
    # 按默认 15 秒估会少报一个数量级）
    "craft": 45.0,
}
# 直连通道（compat）的耗时表：没有 30s 间隔税，官方 API 与本地模型都快一个数量级。
# 两张表必须分开：拿反代表算直连，会把 300 章实际半小时的活报成 3.7 小时；
# 反过来拿直连表算反代，用户会在第 40 分钟以为程序死了。
ETA_PER_TASK_FAST = {
    "draft": 12.0,
    "beats": 4.0,
    "polish": 10.0,
    "deai": 10.0,
    "check": 2.0,
    "summarize": 4.0,
    "extract": 4.0,
    "craft": 8.0,
}
# 并发白名单：只有「读已有内容 + 各写各的结果 + 不共享中间状态」的任务类型能并行
PARALLEL_TASKS = {"craft"}
# 章级拆解的 priority：卷(5)/书(4) 有依赖，必须等本卷章卡跑齐，不参与并发
PARALLEL_MIN_PRIORITY = 6
# 兜底上限：通道没配 max_concurrency 时最多开几路，再多只是在让上游排队
PARALLEL_SLOTS_CAP = 4
# 退避：第一次失败等 30 秒，第二次 120 秒（与设计文档一致）
BACKOFF = (30.0, 120.0)
# 每完成这么多节 draft 就停下来抽检
TRIAGE_EVERY = 10
IDLE_SLEEP = 1.5
# 队列空闲这么久就退出 worker（下次入队自动重启）。常驻轮询每 1.5s 查一次库，
# 单机自用没必要一直占着；但暂停与待抽检时不能退，否则状态就丢了。
IDLE_EXIT_SEC = 600


class JobQueue:
    def __init__(self, store, gateway):
        self.store = store
        self.gateway = gateway
        self._task: Optional[asyncio.Task] = None
        self._stopping = False

        self.paused = False
        self.reason = ""
        # 正在跑的任务：job_id → (job, 开始时刻)。串行时长度是 1，
        # 直连章级拆解的一批里可能同时有几个
        self._running: dict[str, tuple[dict[str, Any], float]] = {}
        self.last_error = ""
        self._last_busy = time.time()

        # 抽检：只保留最近若干条 draft 的分数，够显示样本就行
        self._since_triage = 0
        self._triage: list[dict[str, Any]] = []
        self._triage_verdicts: list[str] = []
        self._triage_due = False

    # ==================== 生命周期 ====================

    @property
    def current_job(self) -> Optional[dict[str, Any]]:
        """并发批次里最早开始的那个任务：给外部和 status 当代表用。"""
        if not self._running:
            return None
        return min(self._running.values(), key=lambda x: x[1])[0]

    async def start(self) -> dict[str, Any]:
        """启动消费循环。进程重启时把半路的 job 退回队列。"""
        resumed = await self.store.reset_running_jobs()
        if resumed:
            logger.warning(f"[jobs] 上次进程遗留 {resumed} 个 running 任务，已退回队列")
        if self._task and not self._task.done():
            return {"resumed": resumed, "already": True}
        self._stopping = False
        self._task = asyncio.create_task(self._loop(), name="studio-job-worker")
        return {"resumed": resumed, "already": False}

    async def stop(self) -> None:
        self._stopping = True
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        self._task = None
        # 正在跑的任务退回队列，重启后能接着干。
        # 取消路径里 _run_job 已经退过一笔，这里只兜「协程没跑到 finally」的情况；
        # requeue 写的就是目标状态，重复执行无害。
        for job_id in list(self._running):
            job, _started = self._running.pop(job_id)
            await self.store.requeue_job(job["id"], error="服务关停，已退回队列")

    # ==================== 控制 ====================

    async def pause(self, reason: str = "") -> None:
        self.paused = True
        self.reason = reason or "手动暂停"

    async def resume(self) -> None:
        self.paused = False
        self.reason = ""
        self._triage_due = False
        self._since_triage = 0
        self._triage_verdicts = []

    async def status(self, work_id: str = "") -> dict[str, Any]:
        counts = await self.store.job_counts(work_id)
        eta = await self._eta(work_id)
        running: list[dict[str, Any]] = []
        for job, started in sorted(self._running.values(), key=lambda x: x[1]):
            node = await self.store.get_node(job["node_id"]) if job.get("node_id") else None
            running.append({
                "job_id": job["id"],
                "task_type": job["task_type"],
                "node_id": job.get("node_id"),
                "title": (node or {}).get("title") or "",
                "elapsed_ms": int((time.time() - started) * 1000),
            })
        return {
            "worker": bool(self._task and not self._task.done()),
            "paused": self.paused,
            "reason": self.reason,
            # current 是「代表任务」，并发批次的全貌看 running：
            # 老前端只读 current，不会因为并行就显示不出东西
            "current": running[0] if running else None,
            "running": running,
            "counts": counts,
            "eta_sec": int(eta),
            "last_error": self.last_error,
            "triage": {
                "due": self._triage_due,
                "samples": self._triage,
                "verdicts": self._triage_verdicts,
                "progress": f"{self._since_triage}/{TRIAGE_EVERY}",
            },
        }

    async def triage(self, verdict: str) -> dict[str, Any]:
        """抽检判定：ok 继续；rewrite 记录；连续两次 rewrite 整条流水线停下。"""
        if verdict not in ("ok", "rewrite"):
            raise ValueError("verdict 只能是 ok 或 rewrite")
        self._triage_verdicts.append(verdict)
        if len(self._triage_verdicts) >= 2 and all(v == "rewrite" for v in self._triage_verdicts[-2:]):
            await self.pause("连续两次抽检判为「重写」：先检查 prompt 或模型选型，别继续烧调用")
            self._triage_due = False
            return await self.status()
        self._triage_due = False
        self._since_triage = 0
        self._triage = []
        self.paused = False
        self.reason = ""
        return await self.status()

    async def _eta(self, work_id: str) -> float:
        """排队 + 在跑任务的预计剩余秒数，按通道与并发度折算。

        并行那部分的墙钟时间约等于「总耗时 / 路数」——不是精确预测（任务长短
        不一、上游也会排队），但比按串行报能少掉一个数量级的误导。
        """
        total = 0.0
        # 500 不是随手写的：百万字的书≈333 章 + 卷/书汇总，取 200 会把后半本
        # 算成零，用户看到的 ETA 会低到离谱（store._MAX_LIMIT 是硬上限）
        rows = await self.store.list_jobs(work_id=work_id, status="queued", limit=500)
        groups: dict[int, float] = {}
        for r in rows:
            slots = self._parallel_slots(r)
            table = ETA_PER_TASK_FAST if slots > 1 else ETA_PER_TASK
            key = slots if slots > 1 else 1
            groups[key] = groups.get(key, 0.0) + table.get(r["task_type"], 15.0)
        for slots, sec in groups.items():
            total += sec / slots
        for job, started in self._running.values():
            table = ETA_PER_TASK_FAST if self._parallel_slots(job) > 1 else ETA_PER_TASK
            total += max(0.0, table.get(job["task_type"], 15.0) - (time.time() - started))
        return total

    def _parallel_slots(self, job: dict[str, Any]) -> int:
        """这个任务允许几路并行；1 = 必须串行。

        三道门槛缺一不可：任务类型在白名单、priority 到章级、解析出的通道是
        直连。任何一道不满足就回到串行老路——判错方向的代价是把风控或质量搭进去。
        """
        if (job.get("task_type") not in PARALLEL_TASKS
                or int(job.get("priority") or 0) < PARALLEL_MIN_PRIORITY):
            return 1
        payload = tasks.payload_of(job)
        model = payload.get("model") or pick_model(self.gateway.reg, None)
        provider = self.gateway.resolve(model)[0]
        adapter = self.gateway.reg.adapters.get(provider)
        if adapter is None or not getattr(adapter.cfg, "compat", False):
            return 1   # 反代多开一路就是多一个页面同时打字，风控风险成倍放大
        return max(1, min(int(getattr(adapter.cfg, "max_concurrency", 1) or 1),
                          PARALLEL_SLOTS_CAP))

    # ==================== 消费循环 ====================

    async def _loop(self) -> None:
        logger.info("[jobs] 任务队列已启动")
        while not self._stopping:
            try:
                if self.paused or self._triage_due:
                    await asyncio.sleep(IDLE_SLEEP)
                    continue
                job = await self.store.claim_job()
                if not job:
                    # 空闲太久就退出，别一直空转；暂停/待抽检时保留 worker
                    if (not self.paused and not self._triage_due
                            and time.time() - self._last_busy > IDLE_EXIT_SEC):
                        logger.info("[jobs] 队列空闲，worker 退出（下次入队自动重启）")
                        return
                    await asyncio.sleep(IDLE_SLEEP)
                    continue
                self._last_busy = time.time()
                await self._run_claimed(job)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - 队列本身不能因为一次异常就死掉
                self.last_error = str(exc)
                logger.exception(f"[jobs] 队列循环异常：{exc}")
                await asyncio.sleep(IDLE_SLEEP)
        logger.info("[jobs] 任务队列已停止")

    async def _run_claimed(self, first: dict[str, Any]) -> None:
        """跑刚领到的任务；章级拆解会顺手凑一批同类任务并行跑。

        为什么凑批而不是开多个 worker 协程：领活顺序、暂停、抽检、退避重试这些
        语义全部留在单循环里，只把「同时能跑几路」局部放大；一旦出问题把
        PARALLEL_TASKS 清空就是原来的串行队列。
        """
        slots = self._parallel_slots(first)
        if slots <= 1:
            await self._run_job(first)
            return
        par: list[dict[str, Any]] = [first]
        seq: list[dict[str, Any]] = []
        while len(par) + len(seq) < slots:
            nxt = await self.store.claim_job(
                task_type=first["task_type"], min_priority=PARALLEL_MIN_PRIORITY)
            if not nxt:
                break
            # 同类但通道变了（刚改过配置、或默认模型回落反代）：不并发的几个排在
            # 批次尾巴上串行跑，不退回队列——退回会白写一次库还搅乱 last_error
            (par if self._parallel_slots(nxt) > 1 else seq).append(nxt)
        for job in seq:
            await self._run_job(job)
        if len(par) == 1:
            await self._run_job(par[0])
        else:
            logger.info(f"[jobs] 直连通道并行 {len(par)} 路：{first['task_type']}")
            await asyncio.gather(*(self._run_job(j) for j in par))

    async def _run_job(self, job: dict[str, Any]) -> None:
        self._running[job["id"]] = (job, time.time())
        self.last_error = ""
        try:
            result = await self._execute(job)
            await self.store.finish_job(job["id"], status="done")
            logger.info(f"[jobs] done {job['task_type']} node={job.get('node_id')} "
                        f"{result.get('chars', '')} {result.get('score', '')}")
            if job["task_type"] == "draft":
                await self._note_draft(job, result)
        except asyncio.CancelledError:
            await self.store.requeue_job(job["id"], error="任务被取消，已退回队列")
            raise
        except Exception as exc:  # noqa: BLE001
            await self._handle_failure(job, exc)
        finally:
            self._running.pop(job["id"], None)

    async def _handle_failure(self, job: dict[str, Any], exc: BaseException) -> None:
        err = f"{type(exc).__name__}: {exc}"
        self.last_error = err
        attempts = int(job.get("attempts") or 1)
        max_attempts = int(job.get("max_attempts") or 2)

        if tasks.is_fatal(exc):
            # 人不在：退回队列（不消耗重试次数），整队停下来等
            await self.store.requeue_job(job["id"], error=err, rebate=True)
            await self.pause(f"上游需要人工处理：{exc}")
            logger.error(f"[jobs] 风控/登录中断，队列已暂停：{err}")
            return

        if attempts < max_attempts:
            delay = BACKOFF[min(attempts - 1, len(BACKOFF) - 1)]
            await self.store.requeue_job(job["id"], delay=delay, error=err)
            logger.warning(f"[jobs] 失败将重试（第 {attempts}/{max_attempts} 次，等 {delay:.0f}s）：{err}")
            return

        await self.store.requeue_job(job["id"], error=err, permanent=True)
        logger.error(f"[jobs] 任务最终失败 {job['task_type']} node={job.get('node_id')}：{err}")

    async def _note_draft(self, job: dict[str, Any], result: dict[str, Any]) -> None:
        """draft 完成后累计抽检进度。

        阈值可从任务参数读（`triage_every`）：默认 10 节一检，
        调试与验收时可以用小值把整条链路跑通。
        """
        payload = tasks.payload_of(job)
        try:
            threshold = int(payload.get("triage_every") or TRIAGE_EVERY)
        except (TypeError, ValueError):
            threshold = TRIAGE_EVERY
        threshold = max(1, threshold)

        self._since_triage += 1
        if self._since_triage < threshold:
            return
        rows = await self.store.list_jobs(work_id=job["work_id"], status="done", limit=200)
        recent = [r for r in rows if r["task_type"] == "draft"][-threshold:]
        runs = await self.store.list_runs(job["work_id"], limit=100)
        score_by_node: dict[str, Any] = {}
        for r in runs:
            if r.get("score") is not None:
                score_by_node.setdefault(r.get("node_id") or "", r["score"])
        samples = []
        for r in recent:
            node = await self.store.get_node(r["node_id"]) if r.get("node_id") else None
            samples.append({
                "node_id": r.get("node_id"),
                "title": (node or {}).get("title") or "",
                "score": score_by_node.get(r.get("node_id") or ""),
            })
        self._triage = samples
        self._triage_due = True
        logger.info(f"[jobs] 已完成 {threshold} 节，暂停等待抽检")

    # ==================== 单次执行 ====================

    async def _template_of(self, work: dict[str, Any]) -> Optional[dict[str, Any]]:
        if not work.get("template_id"):
            return None
        return await self.store.get_template(work["template_id"])

    async def _execute(self, job: dict[str, Any]) -> dict[str, Any]:
        work = await self.store.get_work(job["work_id"])
        if not work:
            raise ValueError(f"作品不存在: {job['work_id']}")
        node = await self.store.get_node(job["node_id"]) if job.get("node_id") else None
        if job.get("node_id") and not node:
            raise ValueError(f"节点不存在: {job['node_id']}")

        payload = tasks.payload_of(job)
        resolved = content_types.resolve(
            work.get("kind") or "article", work.get("scale") or "", work.get("subtype") or "",
            work.get("channel") or "")
        ctx = tasks.TaskContext(
            store=self.store,
            gateway=self.gateway,
            work=work,
            node=node,
            payload=payload,
            resolved=resolved,
            template=await self._template_of(work),
            model=payload.get("model") or pick_model(self.gateway.reg, None),
            mode=payload.get("mode") or "quick",
        )
        return await tasks.run_task(ctx, job["task_type"])
