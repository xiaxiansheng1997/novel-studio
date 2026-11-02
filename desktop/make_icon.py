"""生成桌面图标 `desktop/assets/novel-studio.ico`。

为什么不直接放一张现成的 PNG：
- 图标配色要跟着 `frontend/src/assets/main.css` 的墨夜基线走（背景 #0b0c0f、
  run #d9a45b、正文 #ece9e4），手改图片很容易跟主题脱节；
- .ico 要同时含 16/32/48/256 多档，任务栏、资源管理器、快捷方式各取所需，
  单张 PNG 塞进 .ico 会在小尺寸下发糊。

画法刻意保持"大块面"：左侧一道书脊 + 右侧三行文字，16px 下仍认得出是一本书。

用法（在仓库根目录）：
    .venv\\Scripts\\python.exe desktop\\make_icon.py
"""
from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "desktop" / "assets" / "novel-studio.ico"
SIZES = (256, 128, 64, 48, 32, 16)

BG = (11, 12, 15, 255)  # --bg
PANEL = (17, 19, 24, 255)  # --panel
RUN = (217, 164, 91, 255)  # --run
TEXT = (236, 233, 228, 255)  # --text


def draw_master(size: int = 1024) -> Image.Image:
    """在高分辨率下画一张，再整体缩到目标尺寸——圆角和斜边才不会带锯齿。"""
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    u = size / 1024  # 统一按 1024 网格设计，缩放只改这一个系数

    radius = int(190 * u)
    d.rounded_rectangle([0, 0, size - 1, size - 1], radius=radius, fill=BG)
    # 一圈很细的琥珀描边：深色桌面上纯黑方块会糊进背景
    d.rounded_rectangle(
        [int(6 * u), int(6 * u), size - int(7 * u), size - int(7 * u)],
        radius=radius - int(4 * u),
        outline=RUN,
        width=max(1, int(14 * u)),
    )

    # 书页底：右侧一块略亮的面板色，把书脊和文字分开
    d.rounded_rectangle(
        [int(300 * u), int(180 * u), int(880 * u), int(844 * u)],
        radius=int(48 * u),
        fill=PANEL,
    )
    # 书脊：整条 run 色，最醒目的那一笔
    d.rounded_rectangle(
        [int(168 * u), int(180 * u), int(286 * u), int(844 * u)],
        radius=int(44 * u),
        fill=RUN,
    )

    # 三行正文 + 行尾一枚琥珀光标：一眼是「正在写的稿子」而不是静态列表
    lines = [(360, 320, 800, 380), (360, 470, 720, 530), (360, 620, 680, 680)]
    for x0, y0, x1, y1 in lines:
        d.rounded_rectangle(
            [int(x0 * u), int(y0 * u), int(x1 * u), int(y1 * u)],
            radius=int(30 * u),
            fill=TEXT,
        )
    d.rounded_rectangle(
        [int(712 * u), int(620 * u), int(752 * u), int(680 * u)],
        radius=int(18 * u),
        fill=RUN,
    )
    return img


def save_ico(out: Path = OUT, sizes: tuple[int, ...] = SIZES) -> Path:
    """先缩到 256 主图，再交给 Pillow 生成多档 .ico（ICO 最大条目就是 256）。"""
    master = draw_master().resize((256, 256), Image.Resampling.LANCZOS)
    out.parent.mkdir(parents=True, exist_ok=True)
    master.save(out, format="ICO", sizes=[(s, s) for s in sizes])
    return out


def main() -> int:
    path = save_ico()
    print(f"[icon] {path} ({path.stat().st_size} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
