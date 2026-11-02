"""桌面壳拉起的后端入口：等价于 `python -m uvicorn app.main:app`，外加一条停机控制口。

为什么要有这个文件，而不是直接 `-m uvicorn`：见 `desktop/control.py`。
uvicorn 由我们自己 new 出来，才能拿到 `Server` 对象把 `should_exit` 置真，
走完整的 lifespan 关停（队列退回、浏览器关闭、SQLite 关连接）。

手动跑也完全可以（排查桌面壳起不来的时候）：
    .venv\\Scripts\\python.exe desktop\\serve.py
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
# 直跑脚本时 sys.path[0] 是 desktop/，不补这一句就 import 不到 app 包
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8000

# 优雅关停的封顶：到点就拆残留连接，再把时间留给 lifespan 收尾。
# 不设这一项（uvicorn 默认 None = 无限等）时，一个还开着的 SSE（控制台告警流、
# 写作进度流）就能把关停整卡住；壳那边 30 秒一到就是 taskkill /T /F，
# lifespan 没跑完 →「正在写的节退回队列」这个承诺落空，还会留下带
# SingletonLock 的 Chromium profile。所以宁可断连接也要走完收尾。
GRACEFUL_SHUTDOWN_SEC = 10


def _env_int(name: str, default: int) -> int:
    """读整型环境变量，坏值退回默认值并说一声。

    手动跑这个脚本排查时很容易敲错一个数字，为这个 traceback 不划算。
    """
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        print(f"[serve] 警告：{name}={raw!r} 不是数字，按 {default} 走", flush=True)
        return default


def stop_uvicorn(server) -> None:
    """请 uvicorn 收摊。

    `should_exit` 在 0.34 里是普通 bool，新版本改成了 threading.Event——两种都认，
    升级依赖时不至于变成「静默硬杀」。
    """
    flag = getattr(server, "should_exit", None)
    if hasattr(flag, "set"):
        flag.set()
    else:
        server.should_exit = True


def main() -> int:
    import uvicorn

    from app.main import app as fastapi_app  # 延迟 import：报错要能落到日志里

    host = (os.environ.get("W2A_HOST") or DEFAULT_HOST).strip() or DEFAULT_HOST
    # 监听 0.0.0.0 时控制口仍然只绑回环：那是本机进程之间的通道，不该对外
    control_host = "127.0.0.1"
    control_raw = (os.environ.get("W2A_CONTROL_PORT") or "").strip()
    control_token = (os.environ.get("W2A_CONTROL_TOKEN") or "").strip()

    config = uvicorn.Config(
        fastapi_app,
        host=host,
        port=_env_int("W2A_PORT", DEFAULT_PORT),
        log_level="info",
        # 访问日志交给 app 自己的 RequestLogMiddleware（它按 rid 归类、还落盘），
        # 再打一份 uvicorn 的 access log 只会把 desktop.log 冲成流水账
        access_log=False,
        timeout_graceful_shutdown=GRACEFUL_SHUTDOWN_SEC,
    )
    server = uvicorn.Server(config)

    listener = None
    if control_raw:
        from desktop.control import StopListener

        # "auto" 或坏值 → 传 0 让系统给一个随机端口
        bind_port = _env_int("W2A_CONTROL_PORT", 0) if control_raw != "auto" else 0
        try:
            listener = StopListener(
                on_stop=lambda: stop_uvicorn(server), token=control_token, host=control_host
            )
            bound = listener.bind(bind_port)
            listener.start()
            print(f"[serve] 停机控制口 127.0.0.1:{bound}", flush=True)
        except OSError as exc:
            # 控制口开不出来不致命：壳那边会退到强制结束进程
            print(f"[serve] 警告：停机控制口没开起来（{exc}），关停将走强制路径", flush=True)
            listener = None

    try:
        server.run()
    finally:
        if listener is not None:
            listener.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
