"""Windows 侧的几个小工具。

全部走 ctypes / 系统命令，不引 pywin32：桌面壳要打成一个尽量小的 exe，
多一个二进制依赖就多一分打包风险。非 Windows 上这些函数一律安全降级
（返回 False / 直接放行），这样单测在别的机器上也能跑。
"""
from __future__ import annotations

import ctypes
import os
import subprocess
import sys
import webbrowser
from pathlib import Path

IS_WINDOWS = sys.platform == "win32"

ERROR_ALREADY_EXISTS = 183
MB_OK = 0x0
MB_YESNO = 0x4
MB_ICONINFORMATION = 0x40
MB_ICONWARNING = 0x30
MB_ICONERROR = 0x10
IDYES = 6

# 进程已经退出的返回值（WaitForSingleObject 之外的快速判断用）
STILL_ACTIVE = 259


def acquire_single_instance(name: str = "NovelStudio.Desktop") -> int | None:
    """拿到独占权返回句柄，拿不到（已经有一个在跑）返回 None。

    句柄故意不 CloseHandle：它活到进程结束正好。用 Local\\ 命名而不是 Global\\，
    桌面应用只该跟同一个登录会话里的自己互斥，不该挡住别人在他自己的会话里跑。
    """
    if not IS_WINDOWS:
        return 0
    kernel32 = ctypes.windll.kernel32
    kernel32.GetLastError.restype = ctypes.c_ulong
    # restype 必须写成 c_void_p：默认整数会把句柄按 32 位截，某天就会「单实例失效」
    kernel32.CreateMutexW.restype = ctypes.c_void_p
    handle = kernel32.CreateMutexW(None, False, f"Local\\{name}")
    if not handle:
        # 建不出来（权限/句柄耗尽）就别拦用户：宁可多开一个窗口，也不要有双击没反应
        return 0
    if kernel32.GetLastError() == ERROR_ALREADY_EXISTS:
        return None
    return int(handle)


def message_box(text: str, title: str = "Novel Studio", kind: str = "info") -> bool:
    """弹一个原生提示框。没有图形界面时（CI、远程会话）退化成打印。

    返回用户是否点了「是」；只有 yesno 类型有意义，其它一律 True。
    """
    if not IS_WINDOWS:
        print(f"[{kind}] {title}: {text}")
        return True
    flags = {
        "info": MB_OK | MB_ICONINFORMATION,
        "warn": MB_OK | MB_ICONWARNING,
        "error": MB_OK | MB_ICONERROR,
        "yesno": MB_YESNO | MB_ICONWARNING,
    }[kind]
    result = ctypes.windll.user32.MessageBoxW(None, text, title, flags)
    return result == IDYES


def open_path(path: Path) -> bool:
    """在资源管理器里打开一个文件/目录。"""
    if not IS_WINDOWS:
        return False
    try:
        # explorer 对成功也返回非 0，所以只看「有没有抛」
        subprocess.Popen(["explorer", str(path)])  # noqa: S603 - 固定命令，无用户输入拼接
        return True
    except OSError:
        return False


def open_in_browser(url: str) -> bool:
    try:
        webbrowser.open(url)
        return True
    except Exception:  # noqa: BLE001 - 没有默认浏览器时别把壳带崩
        return False


def find_window_by_title(substring: str) -> int:
    """按标题子串找顶层窗口句柄，找不到返回 0。

    用途有两个：单实例时把已有窗口拽到前台；自检时确认窗口真的建出来了。
    """
    if not IS_WINDOWS:
        return 0
    user32 = ctypes.windll.user32
    user32.GetWindowTextLengthW.restype = ctypes.c_int
    # HWND 是指针宽度，用 c_int 存会把高位抹掉（实测 0x100000123 存进去变成 0x123）
    found = ctypes.c_void_p(0)
    needle = substring.lower()

    @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
    def _enum(hwnd, _lparam):
        if not user32.IsWindowVisible(hwnd):
            return True
        length = user32.GetWindowTextLengthW(hwnd)
        if not length:
            return True
        buf = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buf, length + 1)
        if needle in buf.value.lower():
            found.value = hwnd
            return False  # 停止枚举
        return True

    user32.EnumWindows(_enum, 0)
    return int(found.value or 0)


def process_image(pid: int) -> str:
    """这个 pid 跑的是哪个可执行文件（完整路径）。查不到返回空串。

    用途：强杀前确认身份。pid 会被系统回收，状态文件里记着的 pid 可能已经是
    另一个程序，拿它去 taskkill /T /F 会连带端掉无关的进程树。
    """
    if pid <= 0:
        return ""
    if not IS_WINDOWS:
        try:
            return os.readlink(f"/proc/{pid}/exe")
        except OSError:
            return ""
    kernel32 = ctypes.windll.kernel32
    kernel32.OpenProcess.restype = ctypes.c_void_p
    # PROCESS_QUERY_LIMITED_INFORMATION：不需要SeDebug特权，也读得到镜像路径
    handle = kernel32.OpenProcess(0x1000, False, pid)
    if not handle:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(1024)
        size = ctypes.c_ulong(len(buf))
        if not kernel32.QueryFullProcessImageNameW(ctypes.c_void_p(handle), 0, buf, ctypes.byref(size)):
            return ""
        return buf.value[: size.value]
    finally:
        kernel32.CloseHandle(ctypes.c_void_p(handle))


def bring_to_front(hwnd: int) -> None:
    if not IS_WINDOWS or not hwnd:
        return
    user32 = ctypes.windll.user32
    SW_RESTORE = 9
    user32.ShowWindow(hwnd, SW_RESTORE)
    user32.SetForegroundWindow(hwnd)


def kill_process_tree(pid: int) -> bool:
    """连子进程一起结束（/T），且不响应任何询问（/F）。

    只在优雅停机失败后兜底：Playwright 的 Chromium 是后端的子进程，
    单杀父进程会留下一堆孤儿浏览器和带锁的 profile 目录，下次启动直接打不开。
    """
    if not IS_WINDOWS:
        return False
    try:
        r = subprocess.run(  # noqa: S603 - 固定命令
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        return r.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False
