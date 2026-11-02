"""进程之间的一条「停下来」控制线。

为什么不直接用 signal：
- Windows 上 `os.kill(pid, SIGINT)` 走的是 TerminateProcess，等于硬杀；
- `CTRL_BREAK_EVENT` 要求发送方能 attach 到目标的控制台，而打包成
  `--noconsole` 的 exe 自己没有控制台，脚本模式下从 PowerShell 启动时
  attach 子进程控制台又会把当前 stdout 搅乱。

所以改成：后端在 127.0.0.1 上听一个随机端口，壳连上去发一行 `STOP <token>`，
后端把 uvicorn 的 should_exit 置真。这样 lifespan teardown 一定跑得完——
正在写的章节退回队列、Playwright 的 Chromium 关掉、SQLite 关连接。

协议（都是单行、以 \\n 结束）：
    壳 -> 后端： ``STOP <token>``
    后端 -> 壳： ``OK``（已接受，即将退出） / ``NO``（token 不对）

token 由壳生成、经环境变量传给子进程；壳自己另存一份在 `storage\\desktop\\backend.json`
（那个目录整体不进 git，也不对外），`--stop` 与体检靠它拿到凭据。没这个口令的进程
（比如页面里的脚本玩 DNS rebinding）连上端口也喊不动服务。
"""
from __future__ import annotations

import socket
import threading
import traceback
from typing import Callable

VERB = "STOP"
REPLY_OK = "OK"
REPLY_NO = "NO"

# 一行请求最多读这么多字节：token 再长也超不出，读超了就断，免得被人灌死
MAX_LINE = 256


def build_line(token: str) -> bytes:
    return f"{VERB} {token}\n".encode("utf-8")


def parse_request(raw: bytes, expected_token: str) -> bool:
    """这行请求是不是合法的停机指令。"""
    try:
        text = raw.decode("utf-8", "replace").strip()
    except Exception:  # noqa: BLE001 - 解码失败就当非法，不能抛出去打断监听线程
        return False
    verb, _, token = text.partition(" ")
    if verb != VERB:
        return False
    if not expected_token:
        # 没配 token 就等于本机开放：只在纯本机自用的兜底路径出现
        return True
    return token.strip() == expected_token


class StopListener:
    """后端侧：绑端口、起守护线程收停机指令。

    `bind()` 与 `start()` 分开：绑失败（端口被占）要能在调用方线程里抛出/返回错误，
    放到线程里失败就只剩一行静悄悄的死信。
    """

    def __init__(self, on_stop: Callable[[], None], token: str = "", host: str = "127.0.0.1") -> None:
        self._on_stop = on_stop
        self._token = token
        self._host = host
        self._sock: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._closing = threading.Event()

    @property
    def port(self) -> int:
        if self._sock is None:
            raise RuntimeError("StopListener 还没 bind")
        return int(self._sock.getsockname()[1])

    def bind(self, port: int = 0) -> int:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind((self._host, port))
        s.listen(8)
        s.settimeout(1.0)  # 让 accept 周期性回头看 _closing，close() 才不会卡在退出上
        self._sock = s
        return self.port

    def start(self) -> None:
        if self._sock is None:
            raise RuntimeError("先 bind 再 start")
        self._thread = threading.Thread(target=self._serve, name="desktop-stop", daemon=True)
        self._thread.start()

    def _serve(self) -> None:
        sock = self._sock
        if sock is None:
            return
        while not self._closing.is_set():
            try:
                conn, _ = sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break  # close() 之后自然退出
            try:
                self._handle(conn)
            except Exception:  # noqa: BLE001 - 见下方注释
                # 单次连接的桩子不能带走监听线程：对端提前 RST 后 sendall 会抛，
                # 而这条线程是唯一的优雅停机通道——它一死，之后每一次关窗都只能硬杀。
                traceback.print_exc()
            finally:
                try:
                    conn.close()
                except OSError:
                    pass

    def _handle(self, conn: socket.socket) -> None:
        try:
            raw = conn.recv(MAX_LINE)
        except OSError:
            return
        if parse_request(raw, self._token):
            conn.sendall(f"{REPLY_OK}\n".encode("utf-8"))
            self._closing.set()
            self._on_stop()
        else:
            conn.sendall(f"{REPLY_NO}\n".encode("utf-8"))

    def close(self) -> None:
        self._closing.set()
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None


def send_stop(host: str, port: int, token: str = "", timeout: float = 3.0) -> bool:
    """壳侧：喊一声停机。返回后端是否明确答应（OK）。

    明确答应很重要——如果连上了却被回 NO，说明 token 对不上，硬等它退出只会白等
    满超时，这时候该直接上强制手段。
    """
    try:
        with socket.create_connection((host, port), timeout=timeout) as conn:
            conn.sendall(build_line(token))
            reply = conn.recv(64)
    except OSError:
        return False
    return reply.decode("utf-8", "replace").strip() == REPLY_OK
