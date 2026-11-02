"""后端进程的生杀大权。

这里只做四件事：探活、起进程、等就绪、停下来。「停下来」分三级，一级比一级糙：

1. 控制口 `STOP`：走 uvicorn 正常关停 → lifespan teardown（队列退回 / 关浏览器 / 关库）。
2. `taskkill /T /F`：第 1 级超时或第 1 级被打断时用。必须带 /T，因为 Playwright 的
   Chromium 是后代的子进程；只杀父进程会留下孤儿浏览器，而 profile 目录带着
   SingletonLock，下次启动直接开不了浏览器。

探活用 GET /health：它在 `admin_routes` 里是刻意不鉴权的（供反向代理与监控用），
所以壳不需要为了知道自己起没起来而先去处理口令。
"""
from __future__ import annotations

import csv
import io
import json
import os
import secrets
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from desktop import win32
from desktop.config import READY_TIMEOUT_SEC, STOP_TIMEOUT_SEC, DesktopConfig
from desktop.control import send_stop
from desktop.win32 import IS_WINDOWS, kill_process_tree

CREATE_NO_WINDOW = 0x08000000
# 停机指令走的是本机回环，不跟着 W2A_HOST 一起监听 0.0.0.0
CONTROL_HOST = "127.0.0.1"


@dataclass
class Backend:
    """一个后端子进程 + 与之配套的停机凭据。"""

    proc: subprocess.Popen | None
    control_port: int
    control_token: str
    # managed=False 表示这个服务不是壳起的（比如同事手敲了 python dev.py backend）：
    # 关窗口时绝不能把它杀了，人家可能还在用。
    managed: bool = True

    @property
    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None


def free_port() -> int:
    """要一个当前空闲的端口号。bind(0) 拿到后再释放，存在极小竞争窗口，
    但真撞上了也只是退化成强制停机，不影响功能。"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind((CONTROL_HOST, 0))
        return int(s.getsockname()[1])


def probe(cfg: DesktopConfig, timeout: float = 1.5) -> tuple[bool, dict]:
    """GET /health。返回 (是否健康, 详情)。详情里至少有一段能拿去显示的文本。"""
    req = urllib.request.Request(cfg.health_url, headers=cfg.auth_headers(), method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            payload = json.loads(r.read().decode("utf-8", "replace") or "{}")
    except urllib.error.HTTPError as exc:
        return False, {"error": f"{cfg.health_url} 返回 {exc.code}"}
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return False, {"error": str(exc)}
    return payload.get("status") == "ok", payload


def wait_ready(
    cfg: DesktopConfig,
    backend: Backend | None = None,
    timeout: float = READY_TIMEOUT_SEC,
    poll: float = 0.25,
) -> tuple[bool, str]:
    """等到后端健康为止。

    子进程半路退出要**立刻**失败并带上日志尾巴：不这么做的让人等满 60 秒，
    只会得到一句「起不来」，什么线索都没有。
    """
    deadline = time.monotonic() + timeout
    last_err = "没有响应"
    while time.monotonic() < deadline:
        if backend is not None and backend.proc is not None and backend.proc.poll() is not None:
            return False, (
                f"后端进程已退出（returncode={backend.proc.returncode}）\n{tail(cfg.log_path)}"
            )
        ok, info = probe(cfg)
        if ok:
            return True, ""
        last_err = str(info.get("error") or info)
        time.sleep(poll)
    return False, f"等 {timeout:.0f} 秒仍未就绪：{last_err}\n日志：{cfg.log_path}"


def tail(path: Path, lines: int = 20) -> str:
    """取文件末尾几行。日志没生成、被占用都不能影响主流程。"""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    kept = [ln for ln in text.splitlines() if ln.strip()][-lines:]
    return "\n".join(kept)


def open_log(cfg: DesktopConfig):
    """后端的标准输出/错误重定向到这里：Python 没起来、import 炸了，都只有这里有痕迹。"""
    cfg.storage_dir.mkdir(parents=True, exist_ok=True)
    cfg.log_path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(cfg.log_path, "a", encoding="utf-8", errors="replace", newline="")  # noqa: SIM115
    handle.write(
        f"\n===== {datetime.now().isoformat(timespec='seconds')} 桌面壳启动后端 "
        f"({cfg.base_url}) =====\n"
    )
    handle.flush()
    return handle


def start(cfg: DesktopConfig, serve_script: Path | None = None) -> Backend:
    """起后端。端口已被占用且已经健康时，返回 managed=False 的「借用」句柄。"""
    script = serve_script or (cfg.root / "desktop" / "serve.py")
    python_exe = cfg.python_exe
    if python_exe is None:
        raise FileNotFoundError(
            f"找不到 {cfg.root / '.venv'} 里的 Python。先跑一次 scripts\\setup_desktop.ps1。"
        )
    if not script.is_file():
        raise FileNotFoundError(f"找不到后端入口 {script}")

    control_port = free_port()
    token = secrets.token_hex(16)
    env = dict(os.environ)
    env.update(
        {
            # 让后端自己按 .env 里的 W2A_HOST/PORT 监听；这里显式再传一遍，
            # 免得用户在 shell 里残留的旧变量把端口带偏
            "W2A_HOST": cfg.host,
            "W2A_PORT": str(cfg.port),
            "W2A_STORAGE_DIR": str(cfg.storage_dir),
            "W2A_CONTROL_PORT": str(control_port),
            "W2A_CONTROL_TOKEN": token,
            "PYTHONUNBUFFERED": "1",
            "PYTHONIOENCODING": "utf-8",
            "PYTHONUTF8": "1",
            # 子进程里能看出「这是桌面壳拉起来的」，排查时一眼分明
            "W2A_DESKTOP": "1",
        }
    )
    flags = (subprocess.CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW) if IS_WINDOWS else 0
    handle = open_log(cfg)
    try:
        proc = subprocess.Popen(  # noqa: S603 - 路径来自配置，不接受用户输入拼接
            [str(python_exe), str(script)],
            cwd=str(cfg.root),
            env=env,
            stdout=handle,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            creationflags=flags,
        )
    finally:
        # Popen 已经把句柄交给子进程，父进程这边可以关掉了
        handle.close()
    return Backend(proc=proc, control_port=control_port, control_token=token, managed=True)


def adopt(cfg: DesktopConfig) -> Backend | None:
    """端口上已经有健康的服务就借用它，没有返回 None。"""
    ok, _ = probe(cfg)
    if not ok:
        return None
    return Backend(proc=None, control_port=0, control_token="", managed=False)


def port_is_taken(cfg: DesktopConfig) -> bool:
    """端口有人听但不健康 —— 多半是别的程序占着，直接说清楚比等超时友好。"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        return s.connect_ex((cfg.browse_host, cfg.port)) == 0


def stop(
    cfg: DesktopConfig,
    backend: Backend,
    timeout: float = STOP_TIMEOUT_SEC,
    poll: float = 0.2,
    allow_kill: bool = True,
) -> tuple[str, str]:
    """关停后端，返回 (结局, 说明)。

    结局取值：skipped / exited / graceful / killed / refused。
    `allow_kill=False` 表示调用方认不准这个 pid 是不是壳起的（`--stop` 读的是过期
    状态文件），那就只走控制口——口令对不上的服务绝不能用 taskkill /T /F 去动，
    那一下会连带端掉它整棵进程树。
    """
    if not backend.managed:
        return "skipped", "服务不是桌面壳起的，保持原样"
    if backend.proc is None or backend.proc.poll() is not None:
        return "exited", "后端进程已经不在了"

    accepted = send_stop(CONTROL_HOST, backend.control_port, backend.control_token)
    if accepted:
        if _wait_exit(backend, timeout):
            return "graceful", "已按正常流程关停（队列任务已退回，浏览器已关）"
        reason = f"控制口答应后 {timeout:.0f} 秒仍未退出"
    else:
        reason = "停机控制口无响应"

    pid = backend.proc.pid
    if not allow_kill:
        return "refused", f"{reason}；但 pid={pid} 查不到是哪个程序，不强杀"
    if kill_process_tree(pid):
        _wait_exit(backend, 10)
        return "killed", f"{reason}；已强制结束进程树 pid={pid}"
    # taskkill 都失败（极少见）时自己兜底，至少别把父进程留着
    backend.proc.kill()
    _wait_exit(backend, 5)
    return "killed", f"{reason}；taskkill 失败，已 kill 父进程 pid={pid}"


def _wait_exit(backend: Backend, timeout: float, poll: float = 0.2) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if backend.proc is None or backend.proc.poll() is not None:
            return True
        time.sleep(poll)
    return backend.proc is None or backend.proc.poll() is not None


def queue_counts(cfg: DesktopConfig, timeout: float = 2.0) -> dict | None:
    """写作队列里的排队/运行数。给托盘和自检用，读不到就 None，绝不影响主流程。"""
    req = urllib.request.Request(cfg.url("/studio/queue/status"), headers=cfg.auth_headers())
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            payload = json.loads(r.read().decode("utf-8", "replace") or "{}")
    except (urllib.error.URLError, OSError, ValueError):
        return None
    counts = payload.get("counts")
    return counts if isinstance(counts, dict) else None


# ---------------------------------------------------------- 跨进程的状态文件
#
# 壳与后端是两个进程，`--stop` / 体检又可能是第三个进程。控制口的 token 只在启动
# 那一刻经环境变量传过去，不留个地方记下来，第二个进程就只能「猜」或者硬杀。
# 文件落在 storage/desktop/ 下（.gitignore 已整目录排除），权限跟 .env 同一档。

STATE_RELPATH = Path("desktop") / "backend.json"


def state_path(cfg: DesktopConfig) -> Path:
    return cfg.storage_dir / STATE_RELPATH


def write_state(cfg: DesktopConfig, backend: Backend) -> bool:
    """记下控制口凭据，供 `--stop` / 体检这类**另一个进程**使用。

    写不下去不致命：那只是少了个「壳已经退掉、还能从外部停机」的便利兜底，
    绝不能因为一个不可写的目录让双击没反应。
    """
    if backend.proc is None:
        return False
    payload = {
        "pid": backend.proc.pid,
        "control_port": backend.control_port,
        "control_token": backend.control_token,
        "port": cfg.port,
        # 记下当时用的是哪个解释器：`--stop` 要靠它认回这个 pid 到底是不是自己起的服务
        "python_exe": str(cfg.python_exe or ""),
        "started_at": datetime.now().isoformat(timespec="seconds"),
    }
    path = state_path(cfg)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError as exc:
        print(f"警告：状态文件写不下去（{exc}），`--stop` 只能走强制路径")
        return False
    return True


def read_state(cfg: DesktopConfig) -> dict | None:
    path = state_path(cfg)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def clear_state(cfg: DesktopConfig) -> None:
    try:
        state_path(cfg).unlink()
    except OSError:
        pass  # 本来就没有（或者被杀毒软件锁了）不影响任何功能


def stop_from_state(cfg: DesktopConfig, timeout: float = STOP_TIMEOUT_SEC) -> tuple[str, str]:
    """`--stop` 用：按状态文件里的凭据停掉壳起的后端。

    状态文件是历史快照，可能早就过期（后端被 Ctrl+C 了、壳崩了、pid 被系统回收），
    所以先看 pid 还活不活，再认 /health，两关都过才动手。
    """
    state = read_state(cfg)
    if not state:
        return "skipped", "没有状态文件，本机没有壳起的后端"
    pid = int(state.get("pid") or 0)
    ok, _ = probe(cfg)
    if not ok and not _pid_alive(pid):
        clear_state(cfg)
        return "skipped", f"后端早就不在了（pid={pid}），已清掉状态文件"
    if not _owns_pid(cfg, state, pid):
        # 到这一步要么 :8000 上有健康服务、要么 pid 上还有进程在，但那不是我们起的。
        # 拿旧凭据硬来（控制口必定不应，然后就走到 taskkill）等于替别人关机。
        image = win32.process_image(pid)
        return "refused", (
            f"状态文件里的 pid={pid} 已经不是本项目的后端进程"
            f"（现在跑的是：{image or '查不到'}）\n"
            f"确认后可以手动删掉 {state_path(cfg)}，不要按 pid 强杀。"
        )
    backend = Backend(
        proc=_FakeProcess(pid) if pid else None,
        control_port=int(state.get("control_port") or 0),
        control_token=str(state.get("control_token") or ""),
        managed=True,
    )
    outcome, detail = stop(cfg, backend, timeout=timeout)
    clear_state(cfg)
    return outcome, detail


def _owns_pid(cfg: DesktopConfig, state: dict, pid: int) -> bool:
    """状态文件里的 pid 现在还是不是「用本项目虚拟环境跑起来的那个 Python」。

    只看 pid 活不活不够——pid 会被回收给完全无关的程序。后端的启动器固定是
    `cfg.python_exe`，所以拿进程镜像路径比对：对不上就不是我们起的东西。
    查不到镜像（非 Windows 上没 /proc，或者权限不足）也算不认识，宁可不杀。
    """
    if pid <= 0:
        return False
    expected = str(state.get("python_exe") or "")
    if not expected and cfg.python_exe is not None:
        expected = str(cfg.python_exe)
    if not expected:
        return False
    image = win32.process_image(pid)
    if not image:
        return False
    return _same_path(image, expected)


def _same_path(left: str, right: str) -> bool:
    try:
        left, right = os.path.realpath(left), os.path.realpath(right)
    except OSError:
        pass  # 符号链接链不动就按原串比，不误伤即可
    return os.path.normcase(left) == os.path.normcase(right)


class _FakeProcess:
    """只为复用 `stop()` 的等待逻辑：另一个进程里的 Popen 我们拿不到。"""

    def __init__(self, pid: int) -> None:
        self.pid = pid
        self._dead = False

    def poll(self) -> int | None:
        if self._dead:
            return 0
        alive = _pid_alive(self.pid)
        if not alive:
            self._dead = True
            return 0
        return None

    def kill(self) -> None:
        kill_process_tree(self.pid)
        self._dead = True


def _pid_alive(pid: int) -> bool:
    """不加依赖地查一个进程在不在：Windows 用 tasklist，其余用 signal 0。"""
    if pid <= 0:
        return False
    if IS_WINDOWS:
        try:
            r = subprocess.run(  # noqa: S603 - 固定命令
                ["tasklist", "/FI", f"PID eq {pid}", "/NH", "/FO", "CSV"],
                capture_output=True,
                text=True,
                timeout=10,
            )
        except (OSError, subprocess.TimeoutExpired):
            return True  # 查不了就当还在，宁可多试一次停机
        if r.returncode != 0:
            return True
        # 不能直接 `str(pid) in stdout`：内存列里带千分位（"12,345 K"），
        # 而任何一列碰上这段数字都会误判“还在”。只认 PID 那一列。
        for row in csv.reader(io.StringIO(r.stdout or "")):
            if len(row) >= 2 and row[1].strip() == str(pid):
                return True
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True
    return True


def python_running_here() -> bool:
    """自检用：当前进程是不是真的 Python（打包成 exe 后 sys.executable 指向 exe）。"""
    return not getattr(sys, "frozen", False)
