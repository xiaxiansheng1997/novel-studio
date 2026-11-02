"""Novel Studio 桌面壳。

拆成两半：
- 能被单测的纯逻辑（config / serve 的控制协议 / backend 的起停判定）
- 只有 Windows + 有桌面时才跑得动的壳（launcher.py，含 pywebview 窗口与托盘）

这个包不属于 Web 服务本体：launcher 打成的 exe 里不含 FastAPI/Playwright，
它只负责把 `.venv` 里的后端拉起来、开一个原生窗口、关窗时把后端收干净。
"""
