"""桌面壳的配置：把 `.env` 里的 `W2A_*` 和项目路径归一成一个 DesktopConfig。

这里刻意 **不 import `app.*`**：
- 打包成 exe 时壳不必拖着 FastAPI + Playwright 一起进包；
- 后端起不来（虚拟环境没建、依赖没装）时，壳照样能给出人话报错，而不是
  在 import 阶段就 traceback。
因此 `.env` 用下面这个小解析器读，而不是 pydantic-settings。取值优先级与
`app/config.py` 保持一致：**进程环境变量覆盖 .env 文件**。
"""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8000

# 打开窗口时的落地页，以及托盘/菜单里的两个固定入口
START_PATH = "/studio"
CONSOLE_PATH = "/admin?tab=live"

# 等后端 /health 就绪的上限：要 import playwright、开 SQLite，冷启动慢
READY_TIMEOUT_SEC = 60.0
# 优雅停机等待上限：teardown 里要停队列（正在写的节退回队列）+ 关浏览器
STOP_TIMEOUT_SEC = 30.0


class DesktopConfigError(RuntimeError):
    """配置读不出来。消息一律写成「发生了什么 + 该怎么办」，直接给用户看。"""


def parse_env_text(text: str) -> dict[str, str]:
    """解析 .env 文本：支持 `#` 注释、`export K=V`、单/双引号包裹的值。

    只认第一人称需要的子集，故意不做变量插值——和 pydantic-settings 的行为对齐，
    免得同一个 .env 在壳里和后端里读出两个值。
    """
    out: dict[str, str] = {}
    # BOM 可能出现在文件头，也可能被某些编辑器塞在别处；反正 .env 里它没有合法用途。
    text = text.replace("\ufeff", "")
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        key, sep, value = line.partition("=")
        if not sep:
            continue
        key = key.strip()
        if not key:
            continue
        value = value.split(" #", 1)[0].strip()  # 行尾注释只在有空格+# 时才算
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        out[key] = value
    return out


def read_env_file(path: Path) -> dict[str, str]:
    """读 .env；文件不存在返回空 dict（.env 是本地文件，不进仓库，缺了很正常）。"""
    if not path.is_file():
        return {}
    # utf-8-sig：同事用记事本改过就会带上 BOM，不去掉第一个 key 会变成 \ufeffW2A_HOST
    return parse_env_text(path.read_text(encoding="utf-8-sig", errors="replace"))


def resolve_python(root: Path) -> Path | None:
    """找解释器：优先项目自带的 .venv，其次回退到当前解释器。

    必须优先 .venv —— 后端要 import playwright，系统 Python 里没有。
    """
    candidates = [
        root / ".venv" / "Scripts" / "python.exe",  # Windows
        root / ".venv" / "bin" / "python",  # *nix
        root / "venv" / "Scripts" / "python.exe",
    ]
    for c in candidates:
        if c.is_file():
            return c
    return None


@dataclass(frozen=True)
class DesktopConfig:
    """壳需要知道的一切：路径、地址、口令。"""

    root: Path
    storage_dir: Path
    host: str = DEFAULT_HOST
    port: int = DEFAULT_PORT
    api_key: str = ""

    @property
    def browse_host(self) -> str:
        """给窗口用的主机名。后端可能监听 0.0.0.0/::，但那不能拿来浏览。"""
        if self.host in ("0.0.0.0", "::", "::1", "*"):
            return DEFAULT_HOST
        return self.host

    @property
    def base_url(self) -> str:
        return f"http://{self.browse_host}:{self.port}"

    def url(self, path: str = START_PATH) -> str:
        return f"{self.base_url}{path if path.startswith('/') else '/' + path}"

    @property
    def health_url(self) -> str:
        return self.url("/health")

    @property
    def python_exe(self) -> Path | None:
        return resolve_python(self.root)

    @property
    def log_path(self) -> Path:
        """后端子进程的 stdout/stderr。壳拉起它时把句柄直接递过去了。"""
        return self.storage_dir / "logs" / "desktop.log"

    @property
    def shell_log_path(self) -> Path:
        """壳自己的事件（起停、托盘、异常）。

        为什么不和上面那份合成一个文件：后端子进程拿的是 `open(..., "a")` 的句柄，
        CPython 只在打开那一刻 seek 到末尾，之后每笔写都按自己记着的偏移走。
        壳往同一个文件里插一行，两边的偏移就错开了——实测后端的后续输出会把壳写的
        行直接盖掉。一人一份才写得住。
        """
        return self.storage_dir / "logs" / "desktop-shell.log"

    @property
    def webview_profile(self) -> Path:
        """WebView2 的用户数据目录。

        必须固定：主题选择和 API Key 都存在 localStorage 里，落到临时目录就等于
        每次启动都要重填一次口令。
        """
        return self.storage_dir / "desktop" / "webview2"

    @property
    def frontend_index(self) -> Path:
        return self.root / "frontend" / "dist" / "index.html"

    @property
    def frontend_built(self) -> bool:
        return self.frontend_index.is_file()

    def auth_headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}


def load(root: Path | None | str = None, environ: dict[str, str] | None = None) -> DesktopConfig:
    """组装配置。`environ` 为 None 时用真实环境变量（单测里传字典进来）。"""
    base = project_root(root)
    env = dict(os.environ if environ is None else environ)
    merged = {**read_env_file(base / ".env"), **{k: v for k, v in env.items() if k.startswith("W2A_")}}

    host = (merged.get("W2A_HOST") or DEFAULT_HOST).strip() or DEFAULT_HOST

    raw_port = (merged.get("W2A_PORT") or "").strip() or str(DEFAULT_PORT)
    try:
        port = int(raw_port)
    except ValueError as exc:
        raise DesktopConfigError(
            f".env 里的 W2A_PORT={raw_port!r} 不是数字。改成 1024-65535 的端口号，"
            f"或者直接删掉这一行（默认 {DEFAULT_PORT}）。"
        ) from exc
    if not (1 <= port <= 65535):
        raise DesktopConfigError(f"W2A_PORT={port} 超出范围，改成 1-65535。")

    storage = Path(merged.get("W2A_STORAGE_DIR") or "storage")
    if not storage.is_absolute():
        storage = base / storage

    return DesktopConfig(
        root=base,
        host=host,
        port=port,
        api_key=(merged.get("W2A_API_KEY") or "").strip(),
        storage_dir=storage,
    )


def _has_project_marker(path: Path) -> bool:
    """这个目录是不是项目根：认 desktop/serve.py 这个后端入口文件。"""
    try:
        return (path / "desktop" / "serve.py").is_file()
    except OSError:
        # 路径过长、非法字符、网络盘断开……都当「不是」处理，别把异常抛给弹窗
        return False


def project_root(explicit: str | Path | None = None) -> Path:
    """壳怎么找到项目目录。

    显式给了（快捷方式/命令行带 `--root`）就只认它：写错了要立刻报错，
    而不是悄悄退回另一个目录，跑着一份谁都不知道的数据。
    没给才自动找。

    打成 exe 后 `__file__` 指向解包的临时目录，拿它当项目根就全错了，所以
    frozen 时按「exe 所在目录 → 上一级 → 当前目录」找含 `desktop/serve.py` 的那个
    （装机脚本建的快捷方式会直接带 --root，走到候选目录只是兜底）。
    """
    if explicit:
        path = Path(explicit).expanduser()
        if _has_project_marker(path):
            return path.resolve()
        raise DesktopConfigError(
            f"--root 指向的目录不像 Novel Studio 项目：{path}\n"
            "里面没有 desktop\\serve.py。确认文件夹没被搬走或改名，"
            "或者干脆去掉 --root 让壳自己找。"
        )

    candidates: list[Path] = []
    if getattr(sys, "frozen", False):
        exe = Path(sys.executable).resolve().parent
        candidates += [exe, exe.parent, Path.cwd()]
    else:
        candidates.append(Path(__file__).resolve().parents[1])

    for c in candidates:
        if _has_project_marker(c):
            return c.resolve()
    raise DesktopConfigError(
        "认不出项目目录（找一个含 desktop\\serve.py 的目录）。"
        "如果重命名或搬动过 Novel Studio 文件夹，重新跑一次 scripts\\setup_desktop.ps1，"
        "或者给快捷方式加上 --root <项目目录>。"
    )


def interpreter_for_shell() -> Path | None:
    """没建 .venv 时，脚本模式（python desktop/launcher.py）可以用当前解释器兜底。

    打包成 exe 后 `sys.executable` 指向 exe 自己，绝不能拿去当 Python 用，
    所以 frozen 一律返回 None，让上层报「虚拟环境没建」。
    """
    if getattr(sys, "frozen", False):
        return None
    return Path(sys.executable)
