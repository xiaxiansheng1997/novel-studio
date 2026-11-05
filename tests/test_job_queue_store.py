"""任务入队的两个要命的地方：唯一键会撞、幂等会失效。

`jobs.idem_key` 那列上有 UNIQUE 约束，而写进去的值原本只精到毫秒。生产入队都带业务键
（`work:node:task_type`）所以撞不上；但 `idem_key` 留空时它会退化成
`"task_type#毫秒"` —— 同一毫秒入两笔，两笔的值完全相等，直接 IntegrityError 把请求打成 500。
这个坑在 `test_store_delete.py` 里以偶发的形式露过一次头。
"""
from __future__ import annotations

import pytest

from app.studio import store as studio_store
from app.studio.store import StudioStore


@pytest.fixture
async def store(tmp_path):
    s = StudioStore(tmp_path / "studio.db")
    await s.init()
    try:
        yield s
    finally:
        await s.close()


async def test_same_ms_enqueue_without_idem_key_does_not_collide(store, monkeypatch) -> None:
    """冻住时钟：两笔入队算出的毫秒值必定相等，撞不撞只取决于有没有兜住。"""
    work = await store.create_work(kind="article", title="同一毫秒")
    fixed = 1_700_000_000.123
    monkeypatch.setattr(studio_store, "_now", lambda: fixed)

    a = await store.enqueue_job(work_id=work["id"], node_id=None, task_type="generate")
    b = await store.enqueue_job(work_id=work["id"], node_id=None, task_type="generate")

    assert a["id"] and b["id"], "两笔都该正常落库"
    assert a["id"] != b["id"], "不该撞 UNIQUE 把第二笔弹回来"
    assert a["idem_key"] != b["idem_key"]
    # 业务键与毫秒都还在，只是末尾多了个保唯一的 jid：追溯能力不能因为修碰撞而丢掉
    prefix = f"generate#{int(fixed * 1000)}#"
    assert a["idem_key"].startswith(prefix) and b["idem_key"].startswith(prefix)


async def test_business_key_dedup_still_works(store) -> None:
    """改了存进 idem_key 列的值，也不能动「同业务键 + 未结束」的幂等判定。"""
    work = await store.create_work(kind="article", title="去重")
    idem = f"{work['id']}:draft"

    first = await store.enqueue_job(work_id=work["id"], node_id=None,
                                    task_type="draft", idem_key=idem)
    again = await store.enqueue_job(work_id=work["id"], node_id=None,
                                    task_type="draft", idem_key=idem)

    assert again.get("deduped") is True
    assert again["id"] == first["id"]
    assert len(await store.list_jobs(work_id=work["id"])) == 1


async def test_claim_job_can_filter_type_and_priority(store) -> None:
    """并发批次要能「只领章级拆解」：卷/全书级 priority 更低，不能被顺手领走。

    没有这两个过滤参数，队列就只能要么全串行，要么把有依赖的卷级任务
    和章级一起凑进同一批并行里——后者会稳定报「本卷还有 N 章未拆解」。
    """
    work = await store.create_work(kind="novel", title="过滤")
    vol = await store.enqueue_job(work_id=work["id"], node_id="v1",
                                  task_type="craft", priority=5)
    hot = await store.enqueue_job(work_id=work["id"], node_id=None,
                                  task_type="draft", priority=9)
    chap = await store.enqueue_job(work_id=work["id"], node_id="c1",
                                   task_type="craft", priority=6)

    # 不带过滤：priority 最高的先走
    assert (await store.claim_job())["id"] == hot["id"]
    got = await store.claim_job(task_type="craft", min_priority=6)
    assert got["id"] == chap["id"], "只领章级，不能把卷级捞进并发批次"
    assert await store.claim_job(task_type="craft", min_priority=6) is None
    # 卷级还在排队，等章跑完后由串行路径处理
    assert (await store.claim_job())["id"] == vol["id"]
