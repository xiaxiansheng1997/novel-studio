"""Novel Studio 桌面壳入口。

    .venv\\Scripts\\python.exe desktop\\launcher.py            # 起后端 + 开原生窗口
    .venv\\Scripts\\python.exe desktop\\launcher.py --check    # 只体检，不开窗口不启动
    .venv\\Scripts\\python.exe desktop\\launcher.py --browser  # 用 Edge 应用模式代替原生窗口
    .venv\\Scripts\\python.exe desktop\\launcher.py --stop     # 停掉本机由壳起的后端

打包成 exe 后就是双击图标，行为与第一条命令一致。

生命周期是「点图标才起，关窗口就停」：
    启动 = 起后端子进程 → 隐藏窗口先占好位 → /health 就绪后把窗口亮出来
    退出 = 窗口关闭 → 控制口请后端正常收摊 → 收不干净再强制结束进程树

如果端口上已经有健康的服务在跑（比如同事手敲了 `python dev.py backend`），
壳会借用它、不重复起，关窗口时也绝不杀它。
"""
from __future__ import annotations

import argparse
import importlib.util
import os
import subprocess
import sys
import threading
import time
import traceback
from pathlib import Path

# 直跑脚本（`python desktop\launcher.py`）时 sys.path[0] 是 desktop/，不补这一句
# 就 import 不到 desktop 包；当成模块跑（`python -m desktop.launcher`、打包后的 exe）
# 这句是空操作。
_ROOT = str(Path(__file__).resolve().parents[1])
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from desktop import backend as bk  # noqa: E402
from desktop import win32  # noqa: E402
from desktop.config import CONSOLE_PATH, START_PATH, DesktopConfig, DesktopConfigError, load  # noqa: E402

APP_TITLE = "Novel Studio"
# 首帧底色用墨夜基线：WebView2 建好前的默认色是白，不压一下会白屏一闪
BG_INK = "#0b0c0f"
ICON_PATH = Path(__file__).resolve().parent / "assets" / "novel-studio.ico"
# 体检时分两边查：这三项属于后端子进程（在 .venv 里），这三项属于壳自己
BACKEND_MODULES = ("uvicorn", "fastapi", "playwright")
SHELL_MODULES = ("webview", "pystray", "PIL")


class ShellError(RuntimeError):
    """前置条件不满足。消息会直接进对话框，所以一律写成「该怎么办」。"""


class ShellLogTee:
    """把壳自己的输出**同时**写进 `storage/logs/desktop-shell.log`。

    打成 exe（console=False）后标准输出直接进黑洞：双击没反应时，用户一行日志
    都看不到，只能猜。写日志失败绝不能影响主流程，所以整段吞异常。

    为什么不跟后端共用 `desktop.log`：后端子进程拿着一个 `open(..., "a")` 的句柄，
    CPython 只在打开那一刻 seek 到末尾，以后每笔写都按自己记着的偏移走；壳往同一个
    文件插一行，两边偏移就错开，实测后端的后续输出会把壳写的行直接盖掉。
    """

    def __init__(self, path: Path, sink) -> None:
        self.path = path
        self.sink = sink  # 原来的 sys.stdout：脚本模式下终端仍然看得见
        self._buf = ""
        self._lock = threading.Lock()

    def write(self, text: str) -> int:
        if not text:
            return 0
        with self._lock:
            self._buf += text
            while "\n" in self._buf:
                line, self._buf = self._buf.split("\n", 1)
                self._emit(line)
        return len(text)

    def _emit(self, line: str) -> None:
        if self.sink is not None:
            try:
                self.sink.write(line + "\n")
                self.sink.flush()
            except Exception:  # noqa: BLE001 - 终端被关掉之类的情况不管
                pass
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            stamp = time.strftime("%Y-%m-%d %H:%M:%S")
            with open(self.path, "a", encoding="utf-8", newline="") as fh:
                fh.write(f"{stamp} {line}\n")
        except OSError:
            pass

    def flush(self) -> None:
        with self._lock:
            if self._buf:
                line, self._buf = self._buf, ""
                self._emit(line)

    def isatty(self) -> bool:
        return False


def attach_shell_log(cfg: DesktopConfig) -> None:
    """接管标准输出与标准错误（stderr 复用同一个实例，保证行序）。"""
    try:
        cfg.storage_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        return
    tee = ShellLogTee(cfg.shell_log_path, sys.stdout)
    sys.stdout = tee
    sys.stderr = tee
    # 把解释器一并记下来：用错 Python（系统的 vs .venv 的）是这类壳问题里最高频的一种，
    # 一行日志就能定位，不记下来就得让人去猜为什么「同样的脚本那台机器能跑」。
    print(f"===== {time.strftime('%Y-%m-%dT%H:%M:%S')} 桌面壳启动 "
          f"({'exe' if getattr(sys, 'frozen', False) else '脚本'} · {cfg.base_url}) "
          f"解释器 {sys.executable} =====")


# ---------------------------------------------------------------- 前置检查


def interpreter_mismatch(cfg: DesktopConfig) -> str:
    """当前解释器不是项目 `.venv` 时返回一句人话，没问题返回空串。

    双击 `launcher.py` 会被系统的 Python 接走（.py 的文件关联），而壳依赖只装在
    .venv 里，现象是「窗口弹不出来」。与其等 import 失败再猜，不如在起服务
    之前就拦住：否则后端都被拉起来了才报错，走的人还以为是服务坏了。
    """
    if getattr(sys, "frozen", False):
        return ""  # exe 里没有「解释器」这一回事
    venv = cfg.python_exe
    if venv is None:
        return ""  # 「没建 .venv」由 check_prereq 报，不重复
    if Path(sys.executable).resolve().parent == venv.resolve().parent:
        return ""  # python.exe / pythonw.exe 在同一个 Scripts 目录，都算对
    return (
        "这个壳是用项目外的 Python 跑起来的，那个解释器里没有 pywebview：\n"
        f"    {sys.executable}\n\n"
        "换一种起法（任选其一）：\n"
        "    双击桌面的 Novel Studio 图标（推荐）\n"
        f"    {venv.parent / 'pythonw.exe'} desktop\\launcher.py\n"
        "只想用现在这个 Python 看界面，就加上 --browser 走 Edge 应用模式（不需要 pywebview）。"
    )


def check_prereq(cfg: DesktopConfig) -> None:
    if cfg.python_exe is None:
        raise ShellError(
            f"没有找到虚拟环境的 Python：\n{cfg.root / '.venv'}\n\n"
            "请在项目根目录用 PowerShell 跑一次：\n"
            "    powershell -ExecutionPolicy Bypass -File scripts\\setup_desktop.ps1"
        )
    if not cfg.frontend_built:
        raise ShellError(
            "前端还没构建（缺 frontend\\dist\\index.html），窗口会是白的。\n\n"
            "跑一次：\n    cd frontend ; npm install ; npm run build\n"
            "或者直接重跑 scripts\\setup_desktop.ps1。"
        )


# ---------------------------------------------------------------- 体检


def _use_utf8_console() -> None:
    """体检输出是中文：PowerShell 默认代码页 936，Python 按 UTF-8 写出去就会糊成乱码。

    只在交互控制台有意义（stdout 重定向到文件时 SetConsoleOutputCP 不改变字节，也无伤害）。
    """
    if not win32.IS_WINDOWS:
        return
    try:
        import ctypes

        ctypes.windll.kernel32.SetConsoleOutputCP(65001)
    except Exception:  # noqa: BLE001 - 设不上也不能把体检带倒
        pass


def _modules_in_interpreter(exe: Path, mods: tuple[str, ...]) -> dict[str, bool]:
    """到**目标解释器**里查依赖装了没，返回 {模块: 有没}。查不动就全 False。

    不能用自己进程的 find_spec 判后端依赖：打成 exe 后壳里根本不含
    uvicorn / fastapi / playwright（spec 里成心 excludes 掉的），那样体检会
    永远报 FAIL，把人引去重装一个本来就装好了的东西。后端跑在 .venv 里，
    就得去问 .venv。
    """
    code = (
        "import importlib.util, sys\n"
        "print(';'.join(n + ('=1' if importlib.util.find_spec(n) else '=0') for n in sys.argv[1:]))\n"
    )
    try:
        r = subprocess.run(  # noqa: S603 - 固定命令，参数是模块名
            [str(exe), "-c", code, *mods],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        return {}
    out: dict[str, bool] = {}
    for chunk in (r.stdout or "").strip().split(";"):
        name, sep, flag = chunk.partition("=")
        if sep:
            out[name.strip()] = flag.strip() == "1"
    return out


def doctor(cfg: DesktopConfig) -> int:
    """把「双击没反应」时该看的东西一次列全，给装机的人自己跑。"""
    _use_utf8_console()
    rows: list[tuple[str, bool, str, bool]] = []

    def add(name: str, ok: bool, detail: str = "", required: bool = True) -> None:
        """`required=False` 的行只当参考，不影响退出码（刚装完机后端本来就还没跑）。"""
        rows.append((name, ok, detail, required))

    add("项目目录", True, str(cfg.root))
    py = cfg.python_exe
    add("虚拟环境 Python", py is not None, str(py) if py else f"{cfg.root / '.venv'} 不存在")

    site = cfg.root / ".venv" / "Lib" / "site-packages"
    # 后端依赖只能问 .venv；壳依赖（webview/pystray/PIL）进了 exe，问自己就对了
    backend_found = _modules_in_interpreter(py, BACKEND_MODULES) if py is not None else {}
    for mod in BACKEND_MODULES:
        found = backend_found.get(mod, False)
        add(
            f"后端依赖 {mod}",
            found,
            "" if found else f"用 {py or '.venv\\Scripts\\python.exe'} 跑 pip install -r requirements.txt",
        )
    for mod in SHELL_MODULES:
        found = importlib.util.find_spec(mod) is not None
        add(
            f"壳依赖 {mod}",
            found,
            "" if found else "pip install -r requirements-desktop.txt（跑 exe 时这项应已打包在内）",
        )
    add("虚拟环境目录", site.is_dir() or (cfg.root / ".venv").is_dir(), str(cfg.root / ".venv"))

    add("前端已构建", cfg.frontend_built, str(cfg.frontend_index) if cfg.frontend_built else "npm run build")
    add("存储目录", cfg.storage_dir.is_dir(), str(cfg.storage_dir))

    ok, _info = bk.probe(cfg)
    add(
        "后端在跑",
        ok,
        cfg.base_url if ok else "未运行（正常：双击图标时壳会自己起）",
        required=False,
    )
    if ok:
        counts = bk.queue_counts(cfg)
        add("写作队列", True, str(counts) if counts else "读不到 /studio/queue/status", required=False)

    try:
        import webview  # noqa: F401

        add("pywebview 可导入", True, "原生窗口可用")
    except Exception as exc:  # noqa: BLE001
        add("pywebview 可导入", False, f"{exc} → 只能用 --browser")

    edge = find_edge()
    if win32.IS_WINDOWS:
        add("Edge / WebView2", edge is not None, str(edge) if edge else "缺 WebView2 Runtime，原生窗口可能起不来")

    state = bk.read_state(cfg)
    # 这一行会被截图、被人贴到群里，控制口令不能跟着泄出去
    brief = ""
    if state:
        brief = " ".join(f"{k}={state[k]}" for k in ("pid", "port", "started_at") if state.get(k))
    add("壳状态文件", True, brief or "无（本机没有壳起的后端）", required=False)

    width = max(len(n) for n, _, _, _ in rows)
    for name, ok_flag, detail, _req in rows:
        print(f"{'  ok  ' if ok_flag else '  FAIL'} {name.ljust(width)}  {detail}")
    return 0 if all(flag for _, flag, _, req in rows if req) else 1


# ---------------------------------------------------------------- 壳


def window_failure_text(exc: BaseException) -> str:
    """原生窗口起不来时给人看的说法：先分清「解释器不对」与「WebView2 组件缺失」。

    不分开会指错方向：拿系统的 pythonw.exe 跑 launcher.py 时报
    `ModuleNotFoundError: No module named 'webview'`（壳依赖只装在 .venv 里），
    而文案一律怪 WebView2 组件——人会去重装 runtime，问题一点没动。
    """
    head = f"{type(exc).__name__}: {exc}"
    if isinstance(exc, ImportError):
        return (
            "原生窗口起不来：当前这个 Python 里没装 pywebview。\n\n"
            f"{head}\n\n"
            f"现在用的解释器：{sys.executable}\n"
            "壳的依赖只装在项目的 .venv 里（requirements-desktop.txt），换一种起法：\n"
            "    双击桌面的 Novel Studio 图标（推荐）\n"
            "    或  .venv\\Scripts\\pythonw.exe desktop\\launcher.py\n"
            "没建过 .venv 就先跑一次 scripts\\setup_desktop.ps1。"
        )
    return (
        "原生窗口创建失败（多半是 WebView2 组件不可用）。\n\n" + head
    )


class DesktopShell:
    def __init__(
        self,
        cfg: DesktopConfig,
        *,
        start_path: str = START_PATH,
        use_tray: bool = True,
        debug: bool = False,
    ) -> None:
        self.cfg = cfg
        self.start_path = start_path if start_path.startswith("/") else "/" + start_path
        self.use_tray = use_tray and win32.IS_WINDOWS
        self.debug = debug
        self.backend = bk.Backend(None, 0, "", managed=False)
        self.window = None
        self.icon = None
        self.ready = threading.Event()
        self.quit_requested = threading.Event()
        # 起/停/重启都改 self.backend 与状态文件，得串行：托盘上连点两下「重启服务」，
        # 两个线程各 start 一个子进程，后一个绑不上端口死掉，self.backend 却被覆盖成
        # 死的那个 → 关窗口时只收摊死句柄，前一个健康的后端成了孤儿，“关窗即停”失效。
        # 用可重入锁：restart_service 里面还要调 stop_service。
        self._lifecycle = threading.RLock()

    # ---- 服务 ----

    def start_service(self) -> None:
        with self._lifecycle:
            if bk.adopt(self.cfg) is not None:
                self.backend = bk.Backend(None, 0, "", managed=False)
                print(f"复用已在运行的 {self.cfg.base_url}（关窗口不会停它）")
                return
            if bk.port_is_taken(self.cfg):
                raise ShellError(
                    f"{self.cfg.base_url} 上有别的程序在监听，但它不是 Novel Studio 后端。\n\n"
                    "改掉 .env 里的 W2A_PORT（比如 8010）再试，或者关掉占用端口的程序。"
                )
            self.backend = bk.start(self.cfg)
            bk.write_state(self.cfg, self.backend)
            print(f"后端已拉起 pid={self.backend.proc.pid} → {self.cfg.base_url}")

    def stop_service(self) -> None:
        """只停自己起的。`bk.stop` 本身对「已经不在了」是安全的，所以重复调无副作用。"""
        with self._lifecycle:
            outcome, detail = bk.stop(self.cfg, self.backend)
            if self.backend.managed and outcome != "refused":
                bk.clear_state(self.cfg)
            print(f"关停 {outcome}: {detail}")

    def restart_service(self) -> None:
        with self._lifecycle:
            if not self.backend.managed:
                # 借用别人的后端时，「重启」在这个壳里做不到：stop 会正确地不去杀它，
                # 而紧接着的 start 会在同一端口撞死，探活探到的却是别人家的 /health，
                # 最后报个「重启成功」把一个死进程写进状态文件。
                win32.message_box(
                    f"现在复用的是外部启动的服务（{self.cfg.base_url}），壳管不了它，重启不了。\n\n"
                    "请回到当初启动它的那个窗口（比如 dev.py）里重启，"
                    "或者先把那个进程关掉，再从托盘点一次「重启服务」。",
                    APP_TITLE,
                    "warn",
                )
                return
            self._note("正在重启服务…")
            self.stop_service()
            if self.quit_requested.is_set():
                # 重启途中关了窗口：别再拉新的起来，交给主线程收尾
                return
            try:
                self.backend = bk.start(self.cfg)
                bk.write_state(self.cfg, self.backend)
            except (FileNotFoundError, OSError) as exc:
                win32.message_box(f"服务重启失败：\n{exc}", APP_TITLE, "error")
                return
            ok, detail = bk.wait_ready(self.cfg, self.backend)
            if not ok:
                bk.stop(self.cfg, self.backend)
                bk.clear_state(self.cfg)
                win32.message_box(f"服务重启失败：\n{detail}", APP_TITLE, "error")
                return
            if self.quit_requested.is_set():
                self.stop_service()
                return
            self.goto(self.start_path)
            self._note(f"运行中 · {self.cfg.base_url}")

    def _note(self, text: str) -> None:
        """状态只写进托盘提示和标准输出，不打扰正在打字的人。"""
        print(text)
        if self.icon is not None:
            try:
                self.icon.title = f"{APP_TITLE} · {text}"
            except Exception:  # noqa: BLE001 - 提示文字失败不算事
                pass

    # ---- 页面 ----

    def goto(self, path: str) -> None:
        url = path if path.startswith("http") else self.cfg.url(path)
        if self.window is not None:
            try:
                self.window.load_url(url)
                self.window.show()
                return
            except Exception:  # noqa: BLE001
                pass
        win32.open_in_browser(url)

    def reload_page(self) -> None:
        url = self.cfg.url(self.start_path)
        if self.window is not None:
            try:
                current = self.window.get_current_url()
            except Exception:  # noqa: BLE001
                current = None
            url = current or url
        self.goto(url)

    def open_logs(self) -> None:
        # 壳与后端各一份日志（desktop-shell.log / desktop.log），都在这个目录里
        self.cfg.log_path.parent.mkdir(parents=True, exist_ok=True)
        win32.open_path(self.cfg.log_path.parent)

    def open_data(self) -> None:
        self.cfg.storage_dir.mkdir(parents=True, exist_ok=True)
        win32.open_path(self.cfg.storage_dir)

    def quit(self) -> None:
        self.quit_requested.set()
        if self.window is not None:
            try:
                self.window.destroy()
            except Exception:  # noqa: BLE001 - 窗口都没建好就只当收到退出信号
                pass

    def queue_summary(self) -> str:
        if not self.ready.is_set():
            return "服务启动中…"
        counts = bk.queue_counts(self.cfg)
        if not counts:
            return f"运行中 · {self.cfg.base_url}"
        queued, running = int(counts.get("queued") or 0), int(counts.get("running") or 0)
        if running or queued:
            return f"写作中：跑 {running} · 排队 {queued}"
        return f"空闲 · {self.cfg.base_url}"

    # ---- 托盘 ----

    def _tray_image(self):
        try:
            from PIL import Image

            img = Image.open(ICON_PATH)
            img.size = (32, 32)
            img.load()
            return img.convert("RGBA")
        except Exception:  # noqa: BLE001 - 图标读不出来就不进托盘，别把主流程带崩
            return None

    def start_tray(self) -> bool:
        if not self.use_tray:
            return False
        image = self._tray_image()
        if image is None:
            return False
        try:
            import pystray

            menu = pystray.Menu(
                pystray.MenuItem("显示主窗口", self.show_window, default=True),
                pystray.MenuItem("写作台", lambda: self.goto(START_PATH)),
                pystray.MenuItem("控制台", lambda: self.goto(CONSOLE_PATH)),
                pystray.Menu.SEPARATOR,
                # text 传函数 → 每次展开菜单现读队列状态，不用后台轮询
                pystray.MenuItem(lambda _item: self.queue_summary(), None, enabled=lambda _item: False),
                pystray.MenuItem("重启服务", self._in_thread(self.restart_service)),
                pystray.MenuItem("打开日志目录", self.open_logs),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem("退出", self.quit),
            )
            self.icon = pystray.Icon(APP_TITLE, image, f"{APP_TITLE} · 正在启动", menu)
            self.icon.run_detached()
            print("托盘已挂起（双击回窗口，右键看菜单，退出从这里走）")
            return True
        except Exception:  # noqa: BLE001
            print("托盘起不来（不影响使用）：\n" + traceback.format_exc())
            self.icon = None
            return False

    def show_window(self) -> None:
        if self.window is None:
            return
        try:
            self.window.show()
        except Exception:  # noqa: BLE001
            pass

    def _in_thread(self, fn):
        """菜单/托盘的回调不能堵在 UI 线程上等后端重启，一律另起线程。"""

        def wrapper(*_args) -> None:
            threading.Thread(target=fn, daemon=True).start()

        return wrapper

    # ---- 窗口 ----

    def _window_menu(self):
        from webview.menu import Menu, MenuAction, MenuSeparator

        return [
            Menu(
                "视图",
                [
                    MenuAction("写作台", lambda: self.goto(START_PATH)),
                    MenuAction("控制台", lambda: self.goto(CONSOLE_PATH)),
                    MenuSeparator(),
                    MenuAction("重新载入", self.reload_page),
                ],
            ),
            Menu(
                "服务",
                [
                    MenuAction("打开日志目录", self.open_logs),
                    MenuAction("打开数据目录", self.open_data),
                    MenuSeparator(),
                    MenuAction("重启服务", self._in_thread(self.restart_service)),
                ],
            ),
            Menu("窗口", [MenuAction("退出 Novel Studio", self.quit)]),
        ]

    def run_window(self) -> None:
        import webview

        self.cfg.webview_profile.mkdir(parents=True, exist_ok=True)
        self.window = webview.create_window(
            APP_TITLE,
            url=self.cfg.url(self.start_path),
            width=1400,
            height=900,
            min_size=(900, 600),
            # 后端没就绪前别急着亮出来，否则先看到一张错误页
            hidden=True,
            # 正文编辑器要能选字：pywebview 默认给文档注入 user-select:none
            text_select=True,
            background_color=BG_INK,
            menu=self._window_menu(),
        )
        webview.start(
            self._wait_then_reveal,
            private_mode=False,  # 留住 localStorage：主题选择与 API Key 都存在那里
            storage_path=str(self.cfg.webview_profile),
            debug=self.debug,
        )

    def _wait_then_reveal(self) -> None:
        """webview.start 会把这个函数丢到子线程跑，主线程留给 GUI 消息循环。"""
        ok, detail = bk.wait_ready(self.cfg, self.backend)
        if not ok:
            win32.message_box(
                f"后端没能启动：\n{detail}\n\n"
                f"后端日志：{self.cfg.log_path}\n壳日志：{self.cfg.shell_log_path}",
                APP_TITLE,
                "error",
            )
            self.quit()
            return
        self.ready.set()
        self._note(f"运行中 · {self.cfg.base_url}")
        self.show_window()

    # ---- 浏览器兜底模式 ----

    def run_browser(self) -> int:
        """Edge 应用模式：没有原生菜单，但服务与关停逻辑完全一样。"""
        edge = find_edge()
        if edge is None:
            win32.message_box("找不到 msedge.exe，也无法启动原生窗口（缺 WebView2 Runtime？）。", APP_TITLE, "error")
            return 1
        profile = self.cfg.storage_dir / "desktop" / "edge-app"
        profile.mkdir(parents=True, exist_ok=True)
        subprocess.Popen(  # noqa: S603 - 路径固定，参数里没有用户输入
            [
                str(edge),
                f"--app={self.cfg.url(self.start_path)}",
                f"--user-data-dir={profile}",
                "--no-first-run",
            ]
        )
        self.ready.set()
        self._note("浏览器模式运行中（关掉这个窗口或从托盘退出）")
        # 应用模式的窗口不归壳管（Edge 会把它交给另一个进程），
        # 所以只能等托盘的「退出」或者 Ctrl+C。
        while not self.quit_requested.is_set():
            time.sleep(0.5)
        return 0

    # ---- 主流程 ----

    def run(self) -> int:
        check_prereq(self.cfg)
        wrong = interpreter_mismatch(self.cfg)
        if wrong:
            raise ShellError(wrong)
        self.start_service()
        self.start_tray()
        try:
            self.run_window()
        except Exception as exc:  # noqa: BLE001 - 原生窗口起不来要能落到浏览器兜底
            print("原生窗口起不来：\n" + traceback.format_exc())
            use_browser = win32.message_box(
                window_failure_text(exc) + "\n\n要不要用 Edge 的应用模式打开？没有原生菜单，其余功能一样。",
                APP_TITLE,
                "yesno",
            )
            if not use_browser:
                return 1
            return self.run_browser()
        return 0


def find_edge() -> Path | None:
    roots = [
        os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)"),
        os.environ.get("PROGRAMFILES", r"C:\Program Files"),
        os.environ.get("LOCALAPPDATA", ""),
    ]
    for r in roots:
        if not r:
            continue
        p = Path(r) / "Microsoft" / "Edge" / "Application" / "msedge.exe"
        if p.is_file():
            return p
    return None


# ---------------------------------------------------------------- CLI


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="Novel Studio", description="Novel Studio 桌面壳：双击即起，关窗即停。"
    )
    ap.add_argument("--root", default="", help="项目目录（exe 被搬到别处时指定）")
    ap.add_argument("--check", action="store_true", help="只体检环境，不启动服务")
    ap.add_argument("--stop", action="store_true", help="停掉本机由壳起的后端")
    ap.add_argument("--browser", action="store_true", help="用 Edge 应用模式代替原生窗口")
    ap.add_argument("--no-tray", action="store_true", help="不显示托盘图标")
    ap.add_argument("--path", default=START_PATH, help="启动后打开的路径，默认 /studio")
    ap.add_argument("--debug", action="store_true", help="打开 WebView2 开发者工具")
    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        cfg = load(args.root or None)
    except DesktopConfigError as exc:
        win32.message_box(str(exc), APP_TITLE, "error")
        return 2

    # 先把输出接到日志文件：exe 是无控制台程序，不接上就只能看到一个框或者干脆没反应。
    # --check / --stop 恰恰是「双击没反应」时给人用的入口，它们的输出也得落盘。
    attach_shell_log(cfg)

    if args.check:
        code = doctor(cfg)
        if code != 0 and getattr(sys, "frozen", False):
            win32.message_box(
                "环境体检没通过（有 FAIL 项）。\n\n每行详情在壳日志里：\n"
                f"{cfg.shell_log_path}\n\n把那份日志发回来就能定位。",
                APP_TITLE,
                "error",
            )
        return code

    if args.stop:
        outcome, detail = bk.stop_from_state(cfg)
        print(f"关停 {outcome}: {detail}")
        # 不弹框：--stop 本质上是命令行动作，机器读的是退出码，输出已经进了壳日志。
        # 在这里弹个模态框会把脚本化的调用整条挂死（Start-Process -Wait 就卡在那儿）。
        return 0 if outcome != "refused" else 1

    # 从这里开始是「双击图标」的路径
    if win32.acquire_single_instance() is None:
        hwnd = win32.find_window_by_title(APP_TITLE)
        if hwnd:
            win32.bring_to_front(hwnd)
        else:
            win32.message_box("Novel Studio 已经在运行了（看任务栏右下角托盘）。", APP_TITLE, "info")
        return 0

    shell = DesktopShell(
        cfg, start_path=args.path, use_tray=not args.no_tray, debug=args.debug
    )
    try:
        if args.browser:
            check_prereq(cfg)
            shell.start_service()
            try:
                return shell.run_browser()
            finally:
                shell.stop_service()
        return shell.run()
    except (ShellError, DesktopConfigError, FileNotFoundError) as exc:
        win32.message_box(str(exc), APP_TITLE, "error")
        return 2
    except KeyboardInterrupt:
        return 130
    finally:
        shell.stop_service()
        if shell.icon is not None:
            try:
                shell.icon.stop()
            except Exception:  # noqa: BLE001
                pass
        if isinstance(sys.stdout, ShellLogTee):
            sys.stdout.flush()


if __name__ == "__main__":
    raise SystemExit(main())
