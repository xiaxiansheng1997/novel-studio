"""自动过滑块的安全阀：让位只命中在跑的拖动、让位要真的停手，失败判据不能误伤题干。

这些都直接影响能不能过验证：
- 人手在 /live 里接管时，自动拖动必须真的收手（且不能把没在跑的 provider 也标脏）；
- 一个页面只有一只鼠标，用户的 mousePressed 必须排在自动拖动的 mouse.up 之后；
- 把「操作太频繁，请稍后重试」这类限流/题干文案当成 verifyFail，会让自动拖动在
  还能过的时候提前放弃。
"""
from __future__ import annotations

import asyncio

import pytest

from app.browser import live_browser as lb
from app.browser.live_browser import (
    _drag_human,
    _handoff_requested,
    _has_fail_marker,
    _slide_dragging,
    _slide_handoff,
    _slide_running,
    request_slide_handoff,
    wait_slide_release,
)


class _FakeMouse:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    async def move(self, x: float, y: float) -> None:
        self.calls.append(("move", x, y))

    async def down(self) -> None:
        self.calls.append(("down",))

    async def up(self) -> None:
        self.calls.append(("up",))


class _FakePage:
    def __init__(self) -> None:
        self.mouse = _FakeMouse()


class _StillRandom:
    """把轨迹变成确定值：抖动与停顿归零、采样步数固定，测试不用等真实延时。"""

    @staticmethod
    def uniform(_a: float, _b: float) -> float:
        return 0.0

    @staticmethod
    def randint(a: int, _b: int) -> int:
        return a

    @staticmethod
    def random() -> float:
        return 0.99  # 不触发偶发停顿


@pytest.fixture(autouse=True)
def _clean_state(monkeypatch):
    _slide_running.clear()
    _slide_handoff.clear()
    _slide_dragging.clear()
    monkeypatch.setattr(lb, "random", _StillRandom())
    yield
    _slide_running.clear()
    _slide_handoff.clear()
    _slide_dragging.clear()


def test_handoff_ignores_idle_provider() -> None:
    """没有自动拖动在跑时不该发让位信号（也不该留下脏标志）。"""
    assert request_slide_handoff("qwen") is False
    assert _handoff_requested("qwen") is False


def test_handoff_hits_running_slide() -> None:
    _slide_running["qwen"] = 1
    assert request_slide_handoff("qwen") is True
    assert _handoff_requested("qwen") is True
    # 别的 provider 不受影响
    assert _handoff_requested("doubao") is False


def test_empty_key_never_handoff() -> None:
    """auto_slide_page 允许 key 为空（不属于任何 provider），这种情况不能广播让位。"""
    _slide_running[""] = 1
    assert request_slide_handoff("") is False


def test_fail_markers_only_match_real_reject() -> None:
    assert _has_fail_marker("验证失败，点击框体重试(error:DJ5T1n)") is True
    assert _has_fail_marker("Verifyfail screenshot success") is True
    # 正常题干与限流提示都不能当成「已被判失败」
    assert _has_fail_marker("亲，请拖动下方滑块完成验证") is False
    assert _has_fail_marker("操作太频繁，请稍后重试") is False
    assert _has_fail_marker("") is False


async def test_drag_give_up_does_not_reach_target() -> None:
    """让位后绝不继续拖到 sx+dx：否则日志写着让位，实际又把手柄拖了一遍。"""
    page = _FakePage()
    _slide_handoff.add("qwen")
    await _drag_human(page, 100.0, 50.0, 300.0, key="qwen")

    xs = [c[1] for c in page.mouse.calls if c[0] == "move"]
    assert max(xs) < 300.0  # sx+dx = 400，一格都不该往那边拖
    assert page.mouse.calls[-1] == ("up",), "无论让位与否都必须松开左键"
    assert "qwen" not in _slide_dragging


async def test_drag_without_handoff_completes_drag() -> None:
    """没让位时照常拖到终点再抬手（防止 give_up 分支把正常路径也砍掉）。"""
    page = _FakePage()
    await _drag_human(page, 100.0, 50.0, 300.0, key="qwen")

    assert ("down",) in page.mouse.calls
    assert [c[1] for c in page.mouse.calls if c[0] == "move"][-1] == pytest.approx(400.0)
    assert page.mouse.calls[-1] == ("up",)
    assert "qwen" not in _slide_dragging


async def test_wait_slide_release_returns_when_idle() -> None:
    """没在按住左键（比如停在结果轮询阶段）时不能白等，否则手动输入会被卡住。"""
    assert await wait_slide_release("qwen", timeout=0.05) is True


async def test_wait_slide_release_times_out_while_pressed() -> None:
    _slide_dragging.add("qwen")
    assert await wait_slide_release("qwen", timeout=0.05) is False

    # 自动拖动松开后立刻放行 —— 用户的 mousePressed 必须排在这之后
    task = asyncio.create_task(wait_slide_release("qwen", timeout=2.0))
    await asyncio.sleep(0.05)
    _slide_dragging.discard("qwen")
    assert await task is True
