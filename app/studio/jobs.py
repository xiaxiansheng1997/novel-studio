"""任务队列：单 worker 串行消费，可暂停、可断点续跑。

为什么是「单进程单 worker」而不是线程池：

上游就是浏览器会话，并发只会互相打断页面状态，还会把风控风险成倍放大。
串行跑的代价是慢，但慢是可预期的；并发跑出来的是一堆随机失败，没法排查。

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
    "check": 2.0,
    "summarize": 8.0,
    "extract": 8.0,
}
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
        self.current_job: Optional[dict[str, Any]] = None
        self.current_started = 0.0
        self.last_error = ""
        self._last_busy = time.time()

        # 抽检：只保留最近若干条 draft 的分数，够显示样本就行
        self._since_triage = 0
        self._triage: list[dict[str, Any]] = []
        self._triage_verdicts: list[str] = []
        self._triage_due = False

    # ==================== 生命周期 ====================

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
        # 正在跑的任务退回队列，重启后能接着干
        if self.current_job:
            await self.store.requeue_job(self.current_job["id"], error="服务关停，已退回队列")
            self.current_job = None

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
        queued = counts.get("queued", 0) + counts.get("running", 0)
        eta = 0.0
        if queued:
            rows = await self.store.list_jobs(work_id=work_id, status="queued", limit=200)
            eta = sum(ETA_PER_TASK.get(r["task_type"], 15.0) for r in rows)
            if self.current_job:
                eta += max(0.0, ETA_PER_TASK.get(self.current_job["task_type"], 15.0)
                           - (time.time() - self.current_started))
        current = None
        if self.current_job:
            node = await self.store.get_node(self.current_job["node_id"]) \
                if self.current_job.get("node_id") else None
            current = {
                "job_id": self.current_job["id"],
                "task_type": self.current_job["task_type"],
                "node_id": self.current_job.get("node_id"),
                "title": (node or {}).get("title") or "",
                "elapsed_ms": int((time.time() - self.current_started) * 1000),
            }
        return {
            "worker": bool(self._task and not self._task.done()),
            "paused": self.paused,
            "reason": self.reason,
            "current": current,
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
                await self._run_job(job)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - 队列本身不能因为一次异常就死掉
                self.last_error = str(exc)
                logger.exception(f"[jobs] 队列循环异常：{exc}")
                await asyncio.sleep(IDLE_SLEEP)
        logger.info("[jobs] 任务队列已停止")

    async def _run_job(self, job: dict[str, Any]) -> None:
        self.current_job = job
        self.current_started = time.time()
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
            self.current_job = None
            self.current_started = 0.0

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
        for tpl in await self.store.list_templates():
            if tpl["id"] == work["template_id"]:
                return tpl
        return None

    async def _execute(self, job: dict[str, Any]) -> dict[str, Any]:
        work = await self.store.get_work(job["work_id"])
        if not work:
            raise ValueError(f"作品不存在: {job['work_id']}")
        node = await self.store.get_node(job["node_id"]) if job.get("node_id") else None
        if job.get("node_id") and not node:
            raise ValueError(f"节点不存在: {job['node_id']}")

        payload = tasks.payload_of(job)
        resolved = content_types.resolve(
            work.get("kind") or "article", work.get("scale") or "", work.get("subtype") or "")
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
