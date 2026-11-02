"""桌面壳的可单测部分：配置解析、停机控制协议、关停分级、状态文件。

都是纯逻辑或本机回环 socket，不起真后端、不开窗口（真窗口那部分由
.tmp_patch 下的端到端脚本在 Windows 会话里跑）。
"""
from __future__ import annotations

import io
import socket
import sys
import threading
from pathlib import Path

import pytest
from desktop import backend as bk
from desktop import control
from desktop.config import (
    STOP_TIMEOUT_SEC,
    DesktopConfig,
    DesktopConfigError,
    _has_project_marker,
    load,
    parse_env_text,
    project_root,
)


def make_cfg(tmp_path, **kw) -> DesktopConfig:
    base = {
        "root": tmp_path,
        "storage_dir": tmp_path / "storage",
        "host": "127.0.0.1",
        "port": 8000,
        "api_key": "",
    }
    base.update(kw)
    return DesktopConfig(**base)


# ---------------------------------------------------------------- .env 解析


def test_parse_env_basic():
    text = """
# 注释
W2A_HOST=0.0.0.0
export W2A_PORT=8123
W2A_API_KEY="secret key"   # 行尾注释
W2A_STORAGE_DIR='storage'

BROKEN_LINE
"""
    got = parse_env_text(text)
    assert got["W2A_HOST"] == "0.0.0.0"
    assert got["W2A_PORT"] == "8123"
    assert got["W2A_API_KEY"] == "secret key"
    assert got["W2A_STORAGE_DIR"] == "storage"
    assert "BROKEN_LINE" not in got


def test_load_reads_env_file_and_bom(tmp_path):
    (tmp_path / "desktop").mkdir()
    (tmp_path / "desktop" / "serve.py").write_text("", encoding="utf-8")
    # 记事本改过的 .env 会带 BOM，不能把第一个 key 变成 \ufeffW2A_PORT
    (tmp_path / ".env").write_text("\ufeffW2A_PORT=8200\nW2A_API_KEY=abc\n", encoding="utf-8-sig")
    cfg = load(tmp_path, environ={})
    assert cfg.port == 8200
    assert cfg.api_key == "abc"
    assert cfg.storage_dir == tmp_path / "storage"


def test_load_process_env_wins_over_file(tmp_path):
    (tmp_path / "desktop").mkdir()
    (tmp_path / "desktop" / "serve.py").write_text("", encoding="utf-8")
    (tmp_path / ".env").write_text("W2A_PORT=8200\n", encoding="utf-8")
    cfg = load(tmp_path, environ={"W2A_PORT": "9001"})
    assert cfg.port == 9001


def test_load_bad_port_message_is_actionable(tmp_path):
    (tmp_path / "desktop").mkdir()
    (tmp_path / "desktop" / "serve.py").write_text("", encoding="utf-8")
    (tmp_path / ".env").write_text("W2A_PORT=not-a-number\n", encoding="utf-8")
    with pytest.raises(DesktopConfigError) as exc:
        load(tmp_path, environ={})
    assert "W2A_PORT" in str(exc.value)


def test_project_root_rejects_dir_without_serve(tmp_path):
    with pytest.raises(DesktopConfigError) as exc:
        project_root(tmp_path)
    # 显式 --root 写错时不能悄悄退回别的目录，报错要说清楚是哪个目录
    assert str(tmp_path) in str(exc.value)


def test_project_root_accepts_explicit_and_autodetects(tmp_path):
    (tmp_path / "desktop").mkdir()
    (tmp_path / "desktop" / "serve.py").write_text("", encoding="utf-8")
    assert project_root(tmp_path) == tmp_path.resolve()
    # 不给 --root：脚本模式下应该认得自己所在的项目
    assert _has_project_marker(project_root())


def test_urls_and_browse_host(tmp_path):
    cfg = make_cfg(tmp_path, host="0.0.0.0", port=8010)
    assert cfg.base_url == "http://127.0.0.1:8010"
    assert cfg.health_url == "http://127.0.0.1:8010/health"
    assert cfg.url("/studio") == "http://127.0.0.1:8010/studio"
    assert cfg.auth_headers() == {}
    assert make_cfg(tmp_path, api_key="k").auth_headers() == {"Authorization": "Bearer k"}


# ---------------------------------------------------------------- 控制协议


def test_parse_request_only_accepts_matching_token():
    assert control.parse_request(control.build_line("t0k"), "t0k") is True
    assert control.parse_request(control.build_line("nope"), "t0k") is False
    assert control.parse_request(b"RESTART t0k\n", "t0k") is False
    assert control.parse_request(b"\xff\xfe garbage", "t0k") is False


def test_stop_listener_roundtrip():
    hit = threading.Event()
    listener = control.StopListener(on_stop=hit.set, token="t0k")
    port = listener.bind(0)
    listener.start()
    try:
        assert control.send_stop("127.0.0.1", port, "wrong") is False
        assert not hit.is_set()
        assert control.send_stop("127.0.0.1", port, "t0k") is True
        assert hit.wait(2)
    finally:
        listener.close()


def test_stop_listener_survives_a_crashing_connection():
    """单次连接出岔子绝不能带走监听线程——它是唯一的优雅停机通道。

    线程一死，之后的每一次关窗口都会退化成 taskkill：正在写的章节不再退回队列。
    """
    hit = threading.Event()
    listener = control.StopListener(on_stop=hit.set, token="t0k")
    port = listener.bind(0)
    real_handle = listener._handle
    hits = {"n": 0}
    crashed = threading.Event()

    def flaky(conn):
        hits["n"] += 1
        if hits["n"] == 1:
            crashed.set()
            raise RuntimeError("模拟对端提前 RST 后 sendall 抛")
        real_handle(conn)

    listener._handle = flaky
    listener.start()
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=2) as conn:
            conn.sendall(control.build_line("t0k"))
        assert crashed.wait(2), "监听线程没接到这笔连接"
        assert not hit.is_set()
        # 上一笔把处理函数抛死了，这一笔仍然得能进来
        assert control.send_stop("127.0.0.1", port, "t0k") is True
        assert hit.wait(2)
    finally:
        listener.close()


def test_send_stop_when_nothing_listening():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        dead_port = s.getsockname()[1]
    assert control.send_stop("127.0.0.1", dead_port, "x", timeout=0.5) is False


# ---------------------------------------------------------------- 关停分级


class FakeProc:
    def __init__(self, pid=4242, rc=None):
        self.pid = pid
        self.rc = rc

    def poll(self):
        return self.rc

    def kill(self):
        self.rc = -9


def test_stop_skips_unmanaged(tmp_path):
    cfg = make_cfg(tmp_path)
    backend = bk.Backend(FakeProc(), 1, "t", managed=False)
    assert bk.stop(cfg, backend)[0] == "skipped"


def test_stop_reports_already_exited(tmp_path):
    cfg = make_cfg(tmp_path)
    backend = bk.Backend(FakeProc(rc=0), 1, "t", managed=True)
    assert bk.stop(cfg, backend)[0] == "exited"


def test_stop_graceful_when_control_answers(tmp_path, monkeypatch):
    cfg = make_cfg(tmp_path)
    backend = bk.Backend(FakeProc(), 9, "t", managed=True)
    monkeypatch.setattr(bk, "send_stop", lambda *_a, **_k: True)
    # 第一次 poll 还活着，之后算它退了：模拟 uvicorn 收摊
    polls = iter([None, 0])
    backend.proc.poll = lambda: next(polls, 0)  # type: ignore[method-assign]
    outcome, detail = bk.stop(cfg, backend, timeout=3)
    assert outcome == "graceful", detail


def test_stop_escalates_to_tree_kill(tmp_path, monkeypatch):
    cfg = make_cfg(tmp_path)
    proc = FakeProc()
    backend = bk.Backend(proc, 9, "t", managed=True)
    monkeypatch.setattr(bk, "send_stop", lambda *_a, **_k: False)
    killed = []
    monkeypatch.setattr(bk, "kill_process_tree", lambda pid: killed.append(pid) or True)
    outcome, detail = bk.stop(cfg, backend, timeout=0.5)
    assert outcome == "killed"
    assert killed == [4242]
    assert "控制口" in detail


def test_stop_refuses_to_kill_when_identity_unconfirmed(tmp_path, monkeypatch):
    """调用方认不准这个 pid 是不是自己起的，就不允许走到 taskkill /T /F。"""
    cfg = make_cfg(tmp_path)
    backend = bk.Backend(FakeProc(), 9, "t", managed=True)
    monkeypatch.setattr(bk, "send_stop", lambda *_a, **_k: False)
    killed = []
    monkeypatch.setattr(bk, "kill_process_tree", lambda pid: killed.append(pid) or True)
    outcome, detail = bk.stop(cfg, backend, timeout=0.2, allow_kill=False)
    assert outcome == "refused"
    assert killed == []
    assert "4242" in detail


# ---------------------------------------------------------------- 状态文件


def test_state_roundtrip(tmp_path):
    cfg = make_cfg(tmp_path)
    backend = bk.Backend(FakeProc(pid=777), 4321, "tok", managed=True)
    assert bk.write_state(cfg, backend) is True
    state = bk.read_state(cfg)
    assert state["pid"] == 777 and state["control_port"] == 4321 and state["control_token"] == "tok"
    # 启动器写进状态文件：--stop 靠它认回那个 pid 到底是不是自己起的
    assert state["python_exe"] == str(cfg.python_exe or "")
    # 借用来的后端（proc=None）不该写状态文件
    bk.clear_state(cfg)
    assert bk.write_state(cfg, bk.Backend(None, 1, "x", managed=True)) is False
    assert bk.read_state(cfg) is None


def test_write_state_survives_unwritable_dir(tmp_path):
    """状态文件只是 --stop 的便利兜底，写不下去不能挡住双击启动。"""
    blocked = tmp_path / "storage" / "desktop"
    blocked.parent.mkdir(parents=True, exist_ok=True)
    blocked.write_text("x", encoding="utf-8")  # 目录该在的位置上是个文件
    cfg = make_cfg(tmp_path)
    assert bk.write_state(cfg, bk.Backend(FakeProc(pid=1), 2, "t")) is False


def test_stop_from_state_without_file(tmp_path):
    cfg = make_cfg(tmp_path)
    outcome, detail = bk.stop_from_state(cfg)
    assert outcome == "skipped"
    assert "状态文件" in detail


def test_stop_from_state_refuses_recycled_pid(tmp_path, monkeypatch):
    """壳崩了之后状态文件里的 pid 可能被系统回收给无关程序。

    这时候按旧凭据硬来会走到 taskkill /T /F，连带端掉别人的整棵进程树。
    """
    cfg = make_cfg(tmp_path)
    venv_py = tmp_path / ".venv" / "Scripts" / "python.exe"
    venv_py.parent.mkdir(parents=True, exist_ok=True)
    venv_py.write_text("", encoding="utf-8")
    bk.write_state(cfg, bk.Backend(FakeProc(pid=424242), 4321, "tok", managed=True))
    monkeypatch.setattr(bk, "probe", lambda *_a, **_k: (True, {}))
    monkeypatch.setattr(bk, "_pid_alive", lambda *_a: True)
    monkeypatch.setattr(bk.win32, "process_image", lambda *_a: r"C:\Windows\System32\notepad.exe")
    called = []
    monkeypatch.setattr(bk, "send_stop", lambda *_a, **_k: called.append(1) or False)
    monkeypatch.setattr(bk, "kill_process_tree", lambda pid: called.append(pid) or True)

    outcome, detail = bk.stop_from_state(cfg, timeout=0.2)
    assert outcome == "refused", detail
    assert called == []  # 既不该喊停机，更不该动手杀
    assert "notepad.exe" in detail
    # 不认识就不清状态文件：那是用户唯一能查到旧 pid 的线索
    assert bk.read_state(cfg) is not None


def test_stop_from_state_stops_our_own_python(tmp_path, monkeypatch):
    """镜像对得上就照旧走完整流程，并且清掉状态文件。"""
    cfg = make_cfg(tmp_path)
    venv_py = tmp_path / ".venv" / "Scripts" / "python.exe"
    venv_py.parent.mkdir(parents=True, exist_ok=True)
    venv_py.write_text("", encoding="utf-8")
    bk.write_state(cfg, bk.Backend(FakeProc(pid=424242), 4321, "tok", managed=True))
    monkeypatch.setattr(bk, "probe", lambda *_a, **_k: (True, {}))
    monkeypatch.setattr(bk, "_pid_alive", lambda *_a: True)
    monkeypatch.setattr(bk.win32, "process_image", lambda *_a: str(venv_py))
    seen = {}

    def fake_stop(cfg_, backend, timeout=None):
        seen["token"] = backend.control_token
        seen["managed"] = backend.managed
        return "graceful", "收摊完成"

    monkeypatch.setattr(bk, "stop", fake_stop)
    outcome, _detail = bk.stop_from_state(cfg, timeout=0.2)
    assert outcome == "graceful"
    assert seen == {"token": "tok", "managed": True}
    assert bk.read_state(cfg) is None


def test_pid_alive_reads_only_the_pid_column(monkeypatch):
    """tasklist 的内存列带千分位，拿子串比会误判“还在”。"""
    if not bk.IS_WINDOWS:
        pytest.skip("这条只看 tasklist 的输出格式")

    class R:
        def __init__(self, stdout):
            self.returncode = 0
            self.stdout = stdout

    table = '"chrome.exe","1234","Console","1","42,4242 K"\r\n'
    monkeypatch.setattr(bk.subprocess, "run", lambda *_a, **_k: R(table))
    assert bk._pid_alive(1234) is True
    assert bk._pid_alive(4242) is False  # 4242 只出现在内存列里
    assert bk._pid_alive(0) is False


# ---------------------------------------------------------------- 其它


def test_tail_never_raises(tmp_path):
    cfg = make_cfg(tmp_path)
    assert bk.tail(cfg.log_path) == ""  # 文件还不存在
    cfg.log_path.parent.mkdir(parents=True, exist_ok=True)
    cfg.log_path.write_text("\n".join(f"line{i}" for i in range(50)), encoding="utf-8")
    kept = bk.tail(cfg.log_path, lines=5).splitlines()
    assert kept == ["line45", "line46", "line47", "line48", "line49"]


def test_stop_uvicorn_handles_bool_and_event():
    class AsBool:
        should_exit = False

    class AsEvent:
        def __init__(self):
            class Flag:
                def set(self_inner):
                    self_inner.fired = True

            self.should_exit = Flag()

    from desktop.serve import stop_uvicorn

    a = AsBool()
    stop_uvicorn(a)
    assert a.should_exit is True
    b = AsEvent()
    stop_uvicorn(b)
    assert getattr(b.should_exit, "fired", False) is True


def test_free_port_is_usable():
    port = bk.free_port()
    assert 1024 <= port <= 65535
    with socket.socket() as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        probe.bind(("127.0.0.1", port))


def test_env_int_falls_back_on_bad_value(monkeypatch):
    """手动跑 serve.py 排查时敲错一个数字不该 traceback。"""
    from desktop.serve import _env_int

    monkeypatch.setenv("W2A_PORT", "8123")
    assert _env_int("W2A_PORT", 8000) == 8123
    monkeypatch.setenv("W2A_PORT", "eighty")
    assert _env_int("W2A_PORT", 8000) == 8000
    monkeypatch.delenv("W2A_PORT")
    assert _env_int("W2A_PORT", 8000) == 8000


def test_uvicorn_graceful_shutdown_is_capped():
    """开着 SSE 长连接关窗口时，uvicorn 默认会无限等下去。"""
    from desktop.serve import GRACEFUL_SHUTDOWN_SEC

    # 壳那边 30 秒就硬杀：这个值必须明显小于它，否则永远轮不到走完 lifespan
    assert 0 < GRACEFUL_SHUTDOWN_SEC < STOP_TIMEOUT_SEC


def test_process_image_resolves_current_python():
    """ctypes 签名写错（句柄被按 32 位截）时这个函数会直接拿不到东西。"""
    import os

    from desktop import win32

    if not win32.IS_WINDOWS:
        pytest.skip("这条验的是 Win 侧的 ctypes 签名")
    image = win32.process_image(os.getpid())
    assert image.lower().endswith("python.exe")
    assert win32.process_image(0) == ""


# ---------------------------------------------------------------- 壳行为


def test_restart_service_refuses_borrowed_backend(tmp_path, monkeypatch):
    """复用外部后端时「重启服务」必须说做不到。"""
    from desktop import launcher as L

    cfg = make_cfg(tmp_path)
    shell = L.DesktopShell(cfg, use_tray=False)
    shell.backend = bk.Backend(None, 0, "", managed=False)
    started = []
    monkeypatch.setattr(bk, "start", lambda *_a, **_k: started.append(1))
    boxes = []
    monkeypatch.setattr(L.win32, "message_box", lambda text, *_a, **_k: boxes.append(text) or True)

    shell.restart_service()
    # 不停别人的，也不能在同一端口再拉一个（撞上就会死，探活却探到别人的 /health）
    assert started == []
    assert boxes and "外部启动" in boxes[0]


def test_run_refuses_wrong_python_before_starting_backend(tmp_path, monkeypatch):
    """解释器不对要在起服务之前拦住，不是把后端拉起来之后再弹框。"""
    from desktop import launcher as L

    cfg = make_cfg(tmp_path)
    (tmp_path / ".venv" / "Scripts").mkdir(parents=True)
    (tmp_path / ".venv" / "Scripts" / "python.exe").write_text("", encoding="utf-8")
    (tmp_path / "frontend" / "dist").mkdir(parents=True)
    (tmp_path / "frontend" / "dist" / "index.html").write_text("x", encoding="utf-8")
    monkeypatch.setattr(sys, "executable", str(tmp_path / "other" / "python.exe"))
    started = []
    monkeypatch.setattr(L.bk, "start", lambda *_a, **_k: started.append(1))

    shell = L.DesktopShell(cfg, use_tray=False)
    with pytest.raises(L.ShellError) as exc:
        shell.run()
    assert "pywebview" in str(exc.value)
    assert started == []


def test_lifecycle_lock_serialises_stop(tmp_path):
    """起/停/重启得串行，否则并发的两次重启会留下孤儿后端。"""
    from desktop import launcher as L

    shell = L.DesktopShell(make_cfg(tmp_path), use_tray=False)
    assert shell._lifecycle.acquire(blocking=False)
    done = threading.Event()

    def worker():
        shell.stop_service()
        done.set()

    t = threading.Thread(target=worker, daemon=True)
    t.start()
    assert not done.wait(0.3), "锁拿着时另一路就该排队，不能并行改 self.backend"
    shell._lifecycle.release()
    assert done.wait(3)


def test_doctor_asks_venv_python_about_backend_deps(tmp_path, monkeypatch, capsys):
    """体检不能拿壳自己的解释器判后端依赖。"""
    from desktop import launcher as L

    cfg = make_cfg(tmp_path)
    venv_py = tmp_path / ".venv" / "Scripts" / "python.exe"
    venv_py.parent.mkdir(parents=True, exist_ok=True)
    venv_py.write_text("", encoding="utf-8")
    seen = []

    def fake(exe, mods):
        seen.append((str(exe), tuple(mods)))
        return dict.fromkeys(mods, True)

    monkeypatch.setattr(L, "_modules_in_interpreter", fake)
    monkeypatch.setattr(L.bk, "probe", lambda *_a, **_k: (False, {}))
    L.doctor(cfg)

    out = capsys.readouterr().out
    assert seen, "后端依赖没去问 .venv，而是问了自己（打包后会永远 FAIL）"
    assert seen[0][0] == str(venv_py)
    assert "uvicorn" in seen[0][1] and "fastapi" in seen[0][1]
    # 壳依赖（进了 exe）才该问自己
    assert "webview" not in seen[0][1]
    line = next(ln for ln in out.splitlines() if "后端依赖 uvicorn" in ln)
    assert "FAIL" not in line


def test_doctor_does_not_leak_control_token(tmp_path, monkeypatch, capsys):
    """体检输出会被截图、会被贴到群里：控制口令不能跟着出去。"""
    from desktop import launcher as L

    cfg = make_cfg(tmp_path)
    bk.write_state(cfg, bk.Backend(FakeProc(pid=777), 4321, "sup3rsekret", managed=True))
    monkeypatch.setattr(L, "_modules_in_interpreter", lambda _e, mods: dict.fromkeys(mods, True))
    monkeypatch.setattr(L.bk, "probe", lambda *_a, **_k: (False, {}))
    L.doctor(cfg)

    out = capsys.readouterr().out
    assert "sup3rsekret" not in out
    assert "777" in out


# ---------------------------------------------------------------- 壳日志


def test_shell_log_tee_lines_and_flush(tmp_path):
    """壳的输出要能落盘：exe 是无控制台程序，不落盘就什么都看不见。"""
    from desktop.launcher import ShellLogTee

    path = tmp_path / "logs" / "desktop-shell.log"
    sink = io.StringIO()
    tee = ShellLogTee(path, sink)
    print("上半", end="", file=tee)  # 没换行 → 先攒着，不能拆成两行
    print("下半", file=tee)
    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    assert lines[0].endswith("上半下半")
    tee.flush()
    assert sink.getvalue() == "上半下半\n"


def test_shell_log_tee_survives_unwritable_path(tmp_path):
    """日志写不下去（目录只读、磁盘满、路径上是个文件）不能让壳跟着挂。"""
    from desktop.launcher import ShellLogTee

    blocked = tmp_path / "a-file-not-a-dir"
    blocked.write_text("x", encoding="utf-8")
    log = blocked / "nested" / "log.txt"
    tee = ShellLogTee(log, None)
    print("你好", file=tee)  # sink=None（exe 里等价于没有终端）也得吃得下
    tee.flush()
    assert not log.exists()
    assert tee._buf == ""


def test_shell_log_is_separate_from_backend_log(tmp_path):
    """两份日志必须分家：后端子进程的追加句柄会把壳插进同一文件的行盖掉。"""
    cfg = make_cfg(tmp_path)
    assert cfg.shell_log_path != cfg.log_path
    assert cfg.shell_log_path.parent == cfg.log_path.parent


def test_attach_shell_log_records_the_interpreter(tmp_path):
    """壳日志的会话头得带上解释器：用错 Python 是最高频的壳问题。"""
    from desktop import launcher as L

    cfg = make_cfg(tmp_path)
    real_out, real_err = sys.stdout, sys.stderr
    try:
        L.attach_shell_log(cfg)
    finally:
        sys.stdout, sys.stderr = real_out, real_err
    text = cfg.shell_log_path.read_text(encoding="utf-8")
    assert "桌面壳启动" in text
    assert sys.executable in text


def test_interpreter_mismatch_catches_the_wrong_python(tmp_path, monkeypatch):
    """双击 launcher.py 会被系统 Python 接走，那里没有壳依赖。"""
    from desktop import launcher as L

    cfg = make_cfg(tmp_path)
    venv_py = tmp_path / ".venv" / "Scripts" / "python.exe"
    venv_py.parent.mkdir(parents=True, exist_ok=True)
    venv_py.write_text("", encoding="utf-8")
    monkeypatch.setattr(sys, "executable", str(tmp_path / "WindowsApps" / "python.exe"))
    msg = L.interpreter_mismatch(cfg)
    assert "pywebview" in msg and "pythonw.exe" in msg and "--browser" in msg

    # 同一个 Scripts 目录里的 pythonw.exe 不算用错（快捷方式走的就是它）
    monkeypatch.setattr(sys, "executable", str(venv_py.parent / "pythonw.exe"))
    assert L.interpreter_mismatch(cfg) == ""
    # 没建 .venv：「缺虚拟环境」由 check_prereq 报，不重复报一次
    assert L.interpreter_mismatch(make_cfg(tmp_path / "nowhere")) == ""


def test_window_failure_text_blames_the_interpreter_not_webview2():
    """缺 pywebview 不能怪 WebView2 组件（实测这么告过一次，方向完全错）。"""
    from desktop.launcher import window_failure_text

    text = window_failure_text(ImportError("No module named 'webview'"))
    assert "pywebview" in text and ".venv" in text
    assert sys.executable in text
    assert "WebView2 组件不可用" not in text

    other = window_failure_text(RuntimeError("boom"))
    assert "WebView2 组件不可用" in other
    assert "boom" in other


def test_powershell_scripts_with_chinese_have_utf8_bom():
    """Windows PowerShell 5.1 把无 BOM 的 .ps1 当 ANSI 读。

    中文注释会被读坏，极端情况下整个文件语法都解不过去（实测：字符串缺终止符 +
    意外的标记“}”）。带中文的 .ps1 必须带 UTF-8 BOM。
    """
    root = Path(__file__).resolve().parents[1]
    targets = sorted(list(root.glob("scripts/*.ps1")) + list(root.glob("desktop/*.ps1")))
    assert targets, "一个装机脚本都没找到，这条测试本身不对"
    for path in targets:
        raw = path.read_bytes()
        if not any(ord(ch) > 127 for ch in raw.decode("utf-8", errors="replace")):
            continue  # 纯 ASCII：有没有 BOM 都能被正确解析
        assert raw.startswith(b"\xef\xbb\xbf"), f"{path.name} 含中文但没有 UTF-8 BOM，PS5.1 会读成乱码"
