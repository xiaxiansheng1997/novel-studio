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
