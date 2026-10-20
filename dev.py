"""Web2API 开发/构建辅助脚本。
用法:
  python dev.py frontend   # 启动前端 dev server (Vite)
  python dev.py backend    # 启动后端 (uvicorn, 不带 --reload)
  python dev.py build      # 构建前端
  python dev.py all        # 同时启动前后端
"""
import subprocess
import sys
import os

ROOT = os.path.dirname(os.path.abspath(__file__))
FRONTEND = os.path.join(ROOT, "frontend")
PYTHON = os.path.join(ROOT, ".venv", "Scripts", "python.exe")


def run_backend():
    # 注意: Windows 上不要加 --reload！uvicorn 带 --reload 会切换到
    # SelectorEventLoop，而它不支持创建子进程，Playwright 启动浏览器会直接
    # 抛 NotImplementedError（表现为聊天返回空 stream_error）。
    print("[backend] Starting uvicorn on :8000 (no reload, Playwright requires Proactor loop) ...")
    subprocess.run([PYTHON, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", "8000"], cwd=ROOT)


def run_frontend():
    print("[frontend] Starting vite dev server on :5173 ...")
    subprocess.run(["npx", "vite", "--host", "127.0.0.1"], cwd=FRONTEND, shell=True)


def build_frontend():
    print("[build] Building frontend ...")
    subprocess.run(["npx", "vite", "build"], cwd=FRONTEND, shell=True)
    print("[build] Done. Output: frontend/dist/")


def run_all():
    import threading
    t1 = threading.Thread(target=run_backend, daemon=True)
    t2 = threading.Thread(target=run_frontend, daemon=True)
    t1.start()
    t2.start()
    print("\n=== Web2API Dev ===")
    print("  Frontend: http://127.0.0.1:5173 (Vite, proxy to backend)")
    print("  Backend:  http://127.0.0.1:8000 (FastAPI)")
    print("  Press Ctrl+C to stop.\n")
    try:
        t1.join()
        t2.join()
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "all"
    actions = {"backend": run_backend, "frontend": run_frontend, "build": build_frontend, "all": run_all}
    if cmd in actions:
        actions[cmd]()
    else:
        print(f"Unknown command: {cmd}")
        print(__doc__)
