"""队列的并发调度：只有「直连通道 + 章级拆解」这一种组合能并行。

这里测的是调度，不是任务内容——`_execute` 被换成一个记录并发峰值的假执行器，
所以这些用例断言的是「同一时刻跑了几路、哪些任务被领走、还剩什么在排队」。
之所以值得单开一个文件：并发最容易出的事故不是崩，而是悄悄把不该并行的活
并行了（第 5 章看不到第 4 章摘要），那种稿子跑完才发现，代价是几百次调用。
"""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from app.config import ProviderConfig
from app.studio.jobs import ETA_PER_TASK, ETA_PER_TASK_FAST, JobQueue
from app.studio.store import StudioStore

OLLAMA_MODEL = "ollama-qwen2.5:14b"
QWEN_MODEL = "qwen-plus"


@pytest.fixture
async def store(tmp_path):
    s = StudioStore(tmp_path / "studio.db")
    await s.init()
    try:
        yield s
    finally:
        await s.close()


class FakeGateway:
    """只满足 jobs 对 gateway 的两处依赖：resolve() 与 reg.adapters。"""

    def __init__(self, *, max_concurrency: int = 3) -> None:
        cfg_direct = ProviderConfig(
            key="ollama", compat=True, base_url="http://localhost:11434/v1",
            strip_prefix=True, max_concurrency=max_concurrency,
            models=[{"id": OLLAMA_MODEL}])
        cfg_proxy = ProviderConfig(
            key="qwen", base_url="https://chat.qwen.ai", min_interval_sec=30,
            models=[{"id": QWEN_MODEL}])
        self.reg = SimpleNamespace(adapters={
            "ollama": SimpleNamespace(key="ollama", cfg=cfg_direct),
            "qwen": SimpleNamespace(key="qwen", cfg=cfg_proxy),
        })
        self._owners = {OLLAMA_MODEL: "ollama", QWEN_MODEL: "qwen"}

    def resolve(self, model: str) -> tuple[str, str]:
        return self._owners.get(model, "qwen"), model


async def make_queue(store, **kw) -> JobQueue:
    """造一个只跑过 _execute 的队列：真任务内容这里不需要。"""
    q = JobQueue(store, FakeGateway(**kw))
    state: dict[str, int] = {"now": 0, "peak": 0, "running_peak": 0}

    async def fake_execute(self, job):  # noqa: ANN001 - 替换方法，签名跟着 _execute
        state["now"] += 1
        state["peak"] = max(state["peak"], state["now"])
        state["running_peak"] = max(state["running_peak"], len(self._running))
        await asyncio.sleep(0.02)
        state["now"] -= 1
        return {"chars": 1}

    monkey = pytest.MonkeyPatch()
    monkey.setattr(JobQueue, "_execute", fake_execute)
    return q, state, monkey


async def add_jobs(store, work_id: str, *, task_type: str, priority: int,
                   model: str, n: int) -> list[str]:
    ids = []
    for i in range(n):
        row = await store.enqueue_job(
            work_id=work_id, node_id=f"n_{task_type}_{i}", task_type=task_type,
            payload_json=json.dumps({"level": "chapter", "model": model}),
            priority=priority)
        ids.append(row["id"])
    return ids


async def test_parallel_slots_requires_all_three_gates(store) -> None:
    """任务类型、层级、通道——缺一就不能并行。"""
    q, _state, monkey = await make_queue(store)
    try:
        chapter = {"task_type": "craft", "priority": 6,
                   "payload_json": json.dumps({"model": OLLAMA_MODEL})}
        assert q._parallel_slots(chapter) == 3

        volume = dict(chapter, priority=5)          # 卷级有依赖
        assert q._parallel_slots(volume) == 1
        draft = dict(chapter, task_type="draft")    # 写正文要看到上一章摘要
        assert q._parallel_slots(draft) == 1
        proxy = dict(chapter,
                     payload_json=json.dumps({"model": QWEN_MODEL}))  # 反代怕风控
        assert q._parallel_slots(proxy) == 1
        # 认不出的模型会回落反代主力，同样不该拿到并发
        unknown = dict(chapter, payload_json=json.dumps({"model": "不存在的模型"}))
        assert q._parallel_slots(unknown) == 1
    finally:
        monkey.undo()


async def test_slots_follow_channel_config_and_cap(store) -> None:
    """路数取自通道自己的 max_concurrency，并受兜底上限约束。"""
    q, _state, monkey = await make_queue(store, max_concurrency=1)
    job = {"task_type": "craft", "priority": 6,
           "payload_json": json.dumps({"model": OLLAMA_MODEL})}
    try:
        assert q._parallel_slots(job) == 1, "通道只允许一路时不能硬开"
    finally:
        monkey.undo()
    q8, _s2, monkey2 = await make_queue(store, max_concurrency=8)
    try:
        assert q8._parallel_slots(job) == 4, "再多也只是让上游排队，得有上限"
    finally:
        monkey2.undo()


async def test_batch_of_chapter_craft_runs_in_parallel(store) -> None:
    """6 条章级拆解、3 路通道：一批最多 3 路同时跑，任务全部落地。"""
    q, state, monkey = await make_queue(store)
    work = await store.create_work(kind="novel", title="并行拆解")
    try:
        ids = await add_jobs(store, work["id"], task_type="craft",
                             priority=6, model=OLLAMA_MODEL, n=6)
        first = await store.claim_job()
        await q._run_claimed(first)
        assert state["peak"] == 3, "凑批不该超过通道允许的路数"
        assert state["running_peak"] == 3
        # 一批 3 条，剩下的回到队列等下一批（这里只手动跑了一批）
        left = await store.list_jobs(work_id=work["id"], status="queued")
        assert len(left) == 3
        done = await store.list_jobs(work_id=work["id"], status="done")
        assert len(done) == 3
        assert len(set(ids)) == 6
    finally:
        monkey.undo()


async def test_serial_task_does_not_gather_a_batch(store) -> None:
    """draft 串行：领到一个不能顺手把别的活也跑了，剩下的必须还在排队。"""
    q, state, monkey = await make_queue(store)
    work = await store.create_work(kind="novel", title="串行")
    try:
        await add_jobs(store, work["id"], task_type="draft",
                       priority=5, model=OLLAMA_MODEL, n=4)
        first = await store.claim_job()
        await q._run_claimed(first)
        assert state["peak"] == 1
        assert len(await store.list_jobs(work_id=work["id"], status="queued")) == 3
    finally:
        monkey.undo()


async def test_proxy_channel_stays_serial(store) -> None:
    """同样是章级拆解，走反代就只有一路——这条是风控底线，不能被性能理由绕过。"""
    q, state, monkey = await make_queue(store)
    work = await store.create_work(kind="novel", title="反代")
    try:
        await add_jobs(store, work["id"], task_type="craft",
                       priority=6, model=QWEN_MODEL, n=4)
        first = await store.claim_job()
        await q._run_claimed(first)
        assert state["peak"] == 1
        assert len(await store.list_jobs(work_id=work["id"], status="queued")) == 3
    finally:
        monkey.undo()


async def test_channel_change_mid_batch_runs_the_rest_serially(store) -> None:
    """凑批时混进一条反代任务：尾巴上串行跑掉，不退回队列也不并发。"""
    q, state, monkey = await make_queue(store)
    work = await store.create_work(kind="novel", title="混批")
    try:
        await add_jobs(store, work["id"], task_type="craft",
                       priority=6, model=OLLAMA_MODEL, n=2)
        await store.enqueue_job(
            work_id=work["id"], node_id="n_proxy", task_type="craft",
            payload_json=json.dumps({"level": "chapter", "model": QWEN_MODEL}),
            priority=6)
        first = await store.claim_job()
        await q._run_claimed(first)
        assert state["peak"] == 2, "两条直连并行，第三条串行收尾"
        assert not await store.list_jobs(work_id=work["id"], status="queued")
        assert len(await store.list_jobs(work_id=work["id"], status="done")) == 3
    finally:
        monkey.undo()


async def test_status_reports_whole_running_batch(store) -> None:
    """并发批次的全貌要在 status 里看得见，否则 UI 只显示一路、另两路像没干活。"""
    work = await store.create_work(kind="novel", title="状态")
    seen: dict[str, int] = {"running": 0}
    q = JobQueue(store, FakeGateway())

    async def fake_execute(self, job):
        seen["running"] = max(seen["running"], len(self._running))
        await asyncio.sleep(0.01)
        return {"chars": 1}

    monkey = pytest.MonkeyPatch()
    monkey.setattr(JobQueue, "_execute", fake_execute)
    try:
        await add_jobs(store, work["id"], task_type="craft",
                       priority=6, model=OLLAMA_MODEL, n=4)
        first = await store.claim_job()
        await q._run_claimed(first)
        st = await q.status(work["id"])
        assert seen["running"] == 3
        assert st["running"] == [], "跑完之后不该还挂在在跑列表里"
        assert st["current"] is None
        assert st["counts"].get("done") == 3
    finally:
        monkey.undo()


async def test_eta_uses_fast_table_and_divides_by_lanes(store) -> None:
    """ETA 要认通道也要认并发：报大一个数量级和报小一个数量级一样是骗人。"""
    q, _state, monkey = await make_queue(store)
    n = 300
    try:
        fast_work = await store.create_work(kind="novel", title="直连预估")
        await add_jobs(store, fast_work["id"], task_type="craft",
                       priority=6, model=OLLAMA_MODEL, n=n)
        fast = await q._eta(fast_work["id"])
        assert fast == pytest.approx(n * ETA_PER_TASK_FAST["craft"] / 3, rel=0.02)

        slow_work = await store.create_work(kind="novel", title="反代预估")
        await add_jobs(store, slow_work["id"], task_type="craft",
                       priority=6, model=QWEN_MODEL, n=n)
        slow = await q._eta(slow_work["id"])
        assert slow == pytest.approx(n * ETA_PER_TASK["craft"], rel=0.02)
        assert slow / fast > 10, "两套口径差一个数量级，混用必然误导"
    finally:
        monkey.undo()
