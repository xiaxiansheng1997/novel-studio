# -*- mode: python ; coding: utf-8 -*-
"""Novel Studio 桌面壳的 PyInstaller 配置（onedir）。

只打「壳」：launcher + pywebview + pystray + Pillow。
后端（FastAPI/Playwright/jieba）不打进来 —— 壳是去拉起 `.venv` 里那份 Python 的，
把 app 包塞进 exe 只会让体积翻几倍，还会打出一份和仓库不同步的第二副本。

为什么 onedir 而不是 onefile：
- onefile 每次启动都要把包解到临时目录（1-3 秒），双击图标后要干等；
- pythonnet/CLR 在 onefile 下找运行时目录偶发翻车，onedir 没有这个问题；
- 分发时整个目录跟着 setup 脚本一起拷，用户看到的仍然只有一个快捷方式。

打包（在项目根目录）：
    powershell -ExecutionPolicy Bypass -File desktop\\build.ps1
"""
import os

block_cipher = None
here = SPECPATH  # desktop/
root = os.path.abspath(os.path.join(here, ".."))

a = Analysis(
    [os.path.join(here, "launcher.py")],
    pathex=[root],
    binaries=[],
    # 托盘图标按 `Path(__file__).parent / "assets"` 找，冻结后 __file__ 在解包目录里，
    # 所以必须原样放到 desktop/assets 下
    datas=[(os.path.join(here, "assets", "novel-studio.ico"), os.path.join("desktop", "assets"))],
    hiddenimports=[
        # pystray / pywebview 都是运行时按平台动态挑后端，静态分析看不见
        "pystray._win32",
        "webview.platforms.winforms",
        "webview.platforms.edgechromium",
        "clr",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "tkinter",
        "matplotlib",
        "numpy",
        "pytest",
        # 后端相关的东西一律不许混进来，进来了也只会让 exe 变大
        "fastapi",
        "uvicorn",
        "playwright",
        "jieba",
    ],
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="NovelStudio",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,  # 双击不闪黑框；后端与壳的日志都写进 storage/logs/desktop.log
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=os.path.join(here, "assets", "novel-studio.ico"),
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    name="NovelStudio",
)
