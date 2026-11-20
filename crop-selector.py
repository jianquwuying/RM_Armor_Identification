#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""GUI 选区工具：在图片上选择正方形区域，实时显示到 64x64 点阵屏。

支持把图片二值化后输出为「黑 + 红/蓝/白」三色：
    - 二值化阈值可调（0-255），默认开启；
    - 阈值以上的亮像素（255）可着色为 白 / 红 / 蓝，默认白色；
    - 阈值以下的暗像素（0）保持黑色。

用法：
    cd ~/RGB-Matrix-Px-xx/progress
    ./crop-selector                          # 启动后选择图片
    ./crop-selector -i ~/Pictures/a.jpg      # 直接打开图片
    ./crop-selector -i a.jpg --region 100,200,600 --color red

操作：
    - 拖动选区内部：移动
    - 拖动四角手柄：缩放（始终保持正方形）
    - 方向键：移动 1 像素；Shift+方向键：10 像素
    - + / -：缩放 1 像素；Shift +/-：10 像素
    - C：切换输出颜色 白 → 红 → 蓝
    - T：开关二值化
    - Ctrl-R：输出旋转 90°（0 → 90 → 180 → 270）
    - Enter：立即发送到点阵屏
    - Ctrl-O：打开图片；R：重置选区；Esc：退出
    - 切勿按 Ctrl-Z（会停止 PIO 刷新并锁死 /dev/pio0）

图形界面需要 DISPLAY（在 Pi 桌面上运行，或 ssh -X）。
日常运行不需要 sudo；sudo 仅影响实时线程优先级。
"""

import argparse
import base64
import io
import os
import signal
import sys
import time
import tkinter as tk
import tkinter.font as tkfont
import traceback
from tkinter import filedialog, messagebox, ttk

try:
    from PIL import Image, ImageOps
except ImportError:
    sys.exit("错误：缺少 Pillow。请运行：~/RGB-Matrix-Px-xx/progress/venv/bin/pip install pillow")

try:
    from rgbmatrix import RGBMatrix, RGBMatrixOptions
except ImportError:
    sys.exit("错误：未找到 rgbmatrix Python 绑定。请先运行："
             "~/RGB-Matrix-Px-xx/progress/build-rgbmatrix-binding.sh")

try:
    from rp1_dma_guard import EXIT_STUCK, refresh_is_progressing
except ImportError:
    # 守卫模块缺失时按旧行为运行，不影响正常使用
    EXIT_STUCK = 75

    def refresh_is_progressing(*args, **kwargs):
        return True

MATRIX_SIZE = 64
PREVIEW_PIXELS = 256
PREVIEW_PAD = 4
MIN_SELECTION = 8
HANDLE_HALF = 5
HANDLE_HIT = 16
TICK_MS = 40                 # 选区预览 / 点阵屏更新节流（约 25 fps）
PANEL_W = 360

# ---------------------------------------------------------------- 调色板
BG = "#1b1d21"
PANEL_BG = "#23262b"
CARD_BG = "#2b2f35"
CANVAS_BG = "#0e1013"
FG = "#e9ecef"
MUTED = "#8b929c"
ACCENT = "#3ddc84"
ACCENT_DARK = "#2ab868"
ACCENT_TEXT = "#08240f"
BORDER = "#3a3f47"
BTN_BG = "#34383f"
BTN_HOVER = "#41464f"

COLOR_CHOICES = {
    "white": (255, 255, 255),
    "red": (255, 0, 0),
    "blue": (0, 0, 255),
}
COLOR_NAMES = {"white": "白", "red": "红", "blue": "蓝"}
COLOR_ORDER = ("white", "red", "blue")
PAINT_COLORS = dict(COLOR_CHOICES, black=(0, 0, 0))

BRUSH_COLORS = {
    "white": ("白", "#f2f2f2", "#111111"),
    "black": ("黑", "#111111", "#e8eaed"),
    "red": ("红", "#ff3030", "#111111"),
    "blue": ("蓝", "#3060ff", "#ffffff"),
}
BRUSH_ORDER = ("white", "black", "red", "blue")


class Tooltip:
    """鼠标悬停在控件上时显示说明（参数含义）。"""

    def __init__(self, widget, text, family, delay=350, wraplength=280):
        self.widget = widget
        self.text = text
        self.family = family
        self.delay = delay
        self.wraplength = wraplength
        self._after_id = None
        self._tip = None
        widget.bind("<Enter>", self._schedule, add="+")
        widget.bind("<Leave>", self._hide, add="+")
        widget.bind("<ButtonPress>", self._hide, add="+")

    def _schedule(self, _event=None):
        self._cancel()
        self._after_id = self.widget.after(self.delay, self._show)

    def _cancel(self):
        if self._after_id is not None:
            try:
                self.widget.after_cancel(self._after_id)
            except tk.TclError:
                pass
            self._after_id = None

    def _show(self):
        self._after_id = None
        if self._tip is not None:
            return
        try:
            x = self.widget.winfo_rootx() + 8
            y = self.widget.winfo_rooty() + self.widget.winfo_height() + 6
        except tk.TclError:
            return
        tip = tk.Toplevel(self.widget)
        tip.wm_overrideredirect(True)
        try:
            tip.attributes("-topmost", True)
        except tk.TclError:
            pass
        frame = tk.Frame(tip, bg=BORDER)
        frame.pack()
        tk.Label(
            frame, text=self.text, justify="left", bg=CARD_BG, fg=FG,
            font=(self.family, 9), padx=9, pady=6, wraplength=self.wraplength,
        ).pack(padx=1, pady=1)
        tip.update_idletasks()
        tw, th = tip.winfo_reqwidth(), tip.winfo_reqheight()
        sw, sh = tip.winfo_screenwidth(), tip.winfo_screenheight()
        x = int(clamp(x, 6, max(6, sw - tw - 8)))
        y = int(clamp(y, 6, max(6, sh - th - 8)))
        tip.wm_geometry(f"+{x}+{y}")
        self._tip = tip

    def _hide(self, _event=None):
        self._cancel()
        if self._tip is not None:
            try:
                self._tip.destroy()
            except tk.TclError:
                pass
            self._tip = None


def clamp(value, low, high):
    if value < low:
        return low
    if value > high:
        return high
    return value


def to_rgb(im):
    """转成 RGB；透明背景合成到黑色上，并尊重 EXIF 方向。"""
    im = ImageOps.exif_transpose(im)
    if im.mode in ("RGBA", "LA") or (im.mode == "P" and "transparency" in im.info):
        rgba = im.convert("RGBA")
        background = Image.new("RGBA", rgba.size, (0, 0, 0, 255))
        return Image.alpha_composite(background, rgba).convert("RGB")
    return im.convert("RGB")


def photo_from_pil(master, image):
    """不依赖 PIL.ImageTk：用 Tk 8.6 原生 PNG 支持显示 Pillow 图像。"""
    buf = io.BytesIO()
    image.save(buf, format="PNG")
    data = base64.b64encode(buf.getvalue()).decode("ascii")
    return tk.PhotoImage(master=master, data=data)


def downscale_square(crop, size):
    """把正方形裁剪快速缩放到 size×size。

    直接对超大图做 LANCZOS 很慢（4000px 约 130ms）。先 reduce() 做整数倍
    盒式平均降到约 4×size，再 LANCZOS 收尾，质量接近且快一个数量级。
    """
    factor = max(crop.width, crop.height) // (size * 4)
    if factor >= 2:
        crop = crop.reduce(factor)
    return crop.resize((size, size), Image.Resampling.LANCZOS)


def otsu_threshold(gray):
    """从灰度图直方图估算 Otsu 阈值。"""
    hist = gray.histogram()
    if len(hist) < 256:
        hist = (hist + [0] * 256)[:256]
    total = sum(hist)
    if not total:
        return 128
    sum_all = sum(i * h for i, h in enumerate(hist))
    sum_b = 0.0
    w_b = 0
    best = -1.0
    best_t = 128
    for i, h in enumerate(hist):
        w_b += h
        if w_b == 0:
            continue
        w_f = total - w_b
        if w_f == 0:
            break
        sum_b += i * h
        m_b = sum_b / w_b
        m_f = (sum_all - sum_b) / w_f
        between = w_b * w_f * (m_b - m_f) ** 2
        if between > best:
            best = between
            best_t = i
    # 用「>= 阈值」判定前景，故返回背景类最后一个灰度的下一档，
    # 否则纯黑背景（best_t=0）会被误判为前景而整屏变白。
    return min(best_t + 1, 255)


def build_matrix(brightness):
    options = RGBMatrixOptions()
    options.rows = MATRIX_SIZE
    options.cols = MATRIX_SIZE
    options.chain_length = 1
    options.parallel = 1
    options.hardware_mapping = "regular"
    options.disable_hardware_pulsing = True
    options.gpio_slowdown = 2
    options.rp1_rio = 0          # 0 = RP1 PIO 后端（默认）
    options.brightness = brightness
    options.show_refresh_rate = bool(os.environ.get("CROP_SELECTOR_SHOW_REFRESH"))
    return RGBMatrix(options=options)


def parse_region(text):
    try:
        parts = [int(p.strip()) for p in text.split(",")]
    except ValueError:
        raise argparse.ArgumentTypeError("区域格式应为 X,Y,SIZE")
    if len(parts) != 3:
        raise argparse.ArgumentTypeError("区域格式应为 X,Y,SIZE")
    return tuple(parts)


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="在图片上选择正方形区域，二值化后显示到 64x64 点阵屏",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("-i", "--image", help="启动时打开的图片路径")
    parser.add_argument("-B", "--brightness", type=int, default=100,
                        metavar="0-100", help="初始亮度")
    parser.add_argument("--region", type=parse_region, metavar="X,Y,SIZE",
                        help="初始选区（原图像素坐标，正方形）")
    parser.add_argument("-t", "--threshold", type=int, default=128,
                        metavar="0-255", help="初始二值化阈值")
    parser.add_argument("--color", choices=COLOR_ORDER, default="white",
                        help="二值化后亮像素的输出颜色")
    parser.add_argument("--rotation", type=int, choices=(0, 90, 180, 270),
                        default=0, metavar="0|90|180|270",
                        help="输出旋转角度（顺时针）")
    parser.add_argument("--pwm-bits", type=int, default=None, metavar="1-11",
                        help="点阵屏 PWM 位深（默认沿用库设置）")
    parser.add_argument("--luminance-correct", action=argparse.BooleanOptionalAction,
                        default=None,
                        help="CIE1931 亮度校正（默认沿用库设置）")
    parser.add_argument("--no-binarize", action="store_true",
                        help="启动时关闭二值化（显示原始彩色裁剪）")
    args = parser.parse_args(argv)
    if not 0 <= args.brightness <= 100:
        parser.error("亮度必须在 0-100 之间")
    if not 0 <= args.threshold <= 255:
        parser.error("阈值必须在 0-255 之间")
    if args.pwm_bits is not None and not 1 <= args.pwm_bits <= 11:
        parser.error("PWM 位深必须在 1-11 之间")
    return args


class CropSelectorApp:
    def __init__(self, root, args):
        self.root = root
        self.args = args
        self.image = None
        self.image_path = None
        self.base64 = None          # 二值化/旋转后、未叠加画笔的 64×64
        self.crop64 = None          # 最终输出（含画笔覆盖）
        self.matrix = None
        self.preview_photo = None
        self.canvas_photo = None
        self.disp_scale = 1.0
        self.disp_x = 0
        self.disp_y = 0
        self.disp_w = 0
        self.disp_h = 0
        self.sel_x = 0
        self.sel_y = 0
        self.sel_size = MIN_SELECTION
        self.min_size = MIN_SELECTION
        self.drag_mode = None
        self.drag_start = (0, 0)
        self.drag_sel = (0, 0, 0)
        self.rotation = int(getattr(args, "rotation", 0)) % 360

        # 画笔：键为 64×64 输出网格的 (col, row)，值为 white/black/red/blue
        self.paint = {}
        self._paint_undo = []
        self._pending_undo = None
        self._paint_items = {}      # 左侧画布上的画笔覆盖矩形
        self._last_paint_cell = None
        self.brush_color = "black"

        self._dirty = False
        self._resend = False
        self._closing = False
        self._stop_requested = False
        self._tick_id = None
        self._resize_id = None
        self._cursor = ""
        self._error_shown = False
        self._tooltips = []

        # 画布覆盖层 item（只更新坐标，不反复重建，拖动才顺滑）
        self._shade_items = []
        self._sel_item = None
        self._handle_items = []
        self._label_item = None
        self._label_shadow = None

        # 点阵屏先初始化，这样参数面板能读到库的当前默认值
        try:
            self.matrix = build_matrix(args.brightness)
        except Exception as exc:
            raise RuntimeError(f"无法初始化点阵屏：{exc}") from exc
        self.pwm_bits = int(self.matrix.pwmBits)
        self.luminance_correct = bool(self.matrix.luminanceCorrect)
        if args.pwm_bits is not None:
            self.pwm_bits = int(args.pwm_bits)
        if args.luminance_correct is not None:
            self.luminance_correct = bool(args.luminance_correct)
        self._apply_panel_params()
        print(f"点阵屏已就绪：64x64，PIO 后端（PWM {self.pwm_bits} 位）")

        self._setup_fonts_and_style()
        self._build_ui()
        self._install_signals()

        self.root.protocol("WM_DELETE_WINDOW", self.close)
        self._bind_keys()
        self._schedule_tick()
        self.root.after(150, self._startup)

    # ------------------------------------------------------------------ UI
    def _setup_fonts_and_style(self):
        families = set(tkfont.families(self.root))
        self.family = "Droid Sans Fallback" if "Droid Sans Fallback" in families else "DejaVu Sans"
        self.mono = "DejaVu Sans Mono" if "DejaVu Sans Mono" in families else self.family
        for name in ("TkDefaultFont", "TkTextFont", "TkMenuFont", "TkHeadingFont"):
            tkfont.nametofont(name).configure(family=self.family, size=11)

        self.root.configure(bg=BG)
        style = ttk.Style(self.root)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass

        style.configure(".", background=BG, foreground=FG)
        style.configure("TFrame", background=BG)
        style.configure("Panel.TFrame", background=PANEL_BG)
        style.configure("Card.TFrame", background=CARD_BG)
        style.configure("TLabel", background=BG, foreground=FG)
        style.configure("Panel.TLabel", background=PANEL_BG, foreground=FG)
        style.configure("Title.TLabel", background=PANEL_BG, foreground=FG,
                        font=(self.family, 16, "bold"))
        style.configure("Sub.TLabel", background=PANEL_BG, foreground=MUTED,
                        font=(self.family, 9))
        style.configure("Section.TLabel", background=PANEL_BG, foreground=ACCENT,
                        font=(self.family, 9, "bold"))
        style.configure("Hint.TLabel", background=PANEL_BG, foreground=MUTED,
                        font=(self.family, 9))
        style.configure("Info.TLabel", background=PANEL_BG, foreground=MUTED,
                        font=(self.mono, 9), justify="left")
        style.configure("Value.TLabel", background=PANEL_BG, foreground=ACCENT,
                        font=(self.mono, 10, "bold"))
        style.configure("Status.TLabel", background=PANEL_BG, foreground=MUTED,
                        font=(self.family, 9))

        style.configure("TButton", background=BTN_BG, foreground=FG,
                        borderwidth=0, relief="flat", padding=(10, 6),
                        font=(self.family, 10))
        style.map("TButton",
                  background=[("active", BTN_HOVER), ("pressed", "#4b515b")],
                  foreground=[("disabled", "#5c626b")])
        style.configure("Accent.TButton", background=ACCENT, foreground=ACCENT_TEXT,
                        borderwidth=0, relief="flat", padding=(10, 8),
                        font=(self.family, 10, "bold"))
        style.map("Accent.TButton",
                  background=[("active", ACCENT_DARK), ("pressed", "#21924f")],
                  foreground=[("disabled", "#4a6a57")])
        style.configure("Small.TButton", padding=(6, 4), font=(self.family, 9))

        style.configure("TCheckbutton", background=PANEL_BG, foreground=FG)
        style.map("TCheckbutton", background=[("active", PANEL_BG)])
        style.configure("TRadiobutton", background=PANEL_BG, foreground=FG)
        style.map("TRadiobutton", background=[("active", PANEL_BG)])
        for key, fg in (("White", "#f2f2f2"), ("Red", "#ff5a5a"), ("Blue", "#5aa2ff")):
            style.configure(f"{key}.TRadiobutton", background=PANEL_BG, foreground=fg)
            style.map(f"{key}.TRadiobutton", background=[("active", PANEL_BG)])

        style.configure("Horizontal.TScale", background=PANEL_BG,
                        troughcolor="#3a3f47", borderwidth=0, lightcolor=ACCENT,
                        darkcolor=ACCENT)
        style.configure("TSeparator", background=BORDER)

    def _divider(self, parent, pady=10):
        tk.Frame(parent, bg=BORDER, height=1).pack(fill=tk.X, padx=18, pady=pady)

    def _build_ui(self):
        self.root.title("点阵屏图片选区 — 64×64")
        width, height = 1280, 970
        sw, sh = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
        self.root.geometry(f"{width}x{height}+{max(0, (sw - width) // 2)}+{max(0, (sh - height) // 2)}")
        self.root.minsize(1040, 950)

        outer = ttk.Frame(self.root)
        outer.pack(fill=tk.BOTH, expand=True)

        # ------------------------------------------------------ 左侧画布
        left = ttk.Frame(outer)
        left.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        self.canvas = tk.Canvas(left, bg=CANVAS_BG, highlightthickness=1,
                                highlightbackground=BORDER, highlightcolor=BORDER)
        self.canvas.pack(fill=tk.BOTH, expand=True, padx=(12, 6), pady=(12, 6))
        self.canvas.bind("<ButtonPress-1>", self._on_press)
        self.canvas.bind("<B1-Motion>", self._on_drag)
        self.canvas.bind("<ButtonRelease-1>", self._on_release)
        self.canvas.bind("<Motion>", self._on_motion)
        self.canvas.bind("<Configure>", self._on_configure)

        status = ttk.Frame(left, style="Panel.TFrame")
        status.pack(fill=tk.X, padx=(12, 6), pady=(0, 12))
        ttk.Label(
            status,
            text=("拖动/四角缩放　·　方向键微调 (Shift×10)　·　P 画笔　·　"
                  "C 颜色　·　T 二值化　·　Ctrl-R 旋转　·　Enter 发送　·　Esc 退出"),
            style="Status.TLabel",
        ).pack(anchor="w", padx=10, pady=6)

        # ------------------------------------------------------ 右侧面板
        right = tk.Frame(outer, bg=PANEL_BG, width=PANEL_W,
                         highlightthickness=0)
        right.pack(side=tk.RIGHT, fill=tk.Y, padx=(6, 12), pady=12)
        right.pack_propagate(False)
        self.right_panel = right

        tk.Frame(right, bg=ACCENT, height=3).pack(fill=tk.X)

        head = ttk.Frame(right, style="Panel.TFrame")
        head.pack(fill=tk.X, padx=18, pady=(12, 6))
        title_col = ttk.Frame(head, style="Panel.TFrame")
        title_col.pack(side=tk.LEFT, anchor="w")
        ttk.Label(title_col, text="点阵屏选区", style="Title.TLabel").pack(anchor="w")
        ttk.Label(title_col, text="64 × 64 · RP1 PIO · 实时预览",
                  style="Sub.TLabel").pack(anchor="w", pady=(1, 0))
        self.live_var = tk.BooleanVar(value=True)
        self.live_check = ttk.Checkbutton(
            head, text="实时", variable=self.live_var,
            style="TCheckbutton", command=self._on_live_toggle,
        )
        self.live_check.pack(side=tk.RIGHT, anchor="n", pady=(2, 0))

        # 预览卡片
        self.preview_card = tk.Frame(right, bg=CARD_BG, highlightthickness=1,
                                     highlightbackground=BORDER)
        self.preview_card.pack(padx=18, pady=(4, 6))
        self.preview_canvas = tk.Canvas(
            self.preview_card, width=PREVIEW_PIXELS + PREVIEW_PAD * 2,
            height=PREVIEW_PIXELS + PREVIEW_PAD * 2,
            bg="#000000", highlightthickness=0,
        )
        self.preview_canvas.pack(padx=5, pady=5)
        self.preview_canvas.bind("<ButtonPress-1>", self._on_preview_press)
        self.preview_canvas.bind("<B1-Motion>", self._on_preview_drag)
        self.preview_canvas.bind("<ButtonRelease-1>", self._on_preview_release)

        self.info_var = tk.StringVar(value="未打开图片")
        ttk.Label(right, textvariable=self.info_var, style="Info.TLabel",
                  wraplength=324).pack(anchor="w", padx=18)

        self._divider(right, pady=(9, 7))

        # -------------------------------------------------- 输出（颜色/二值化）
        ttk.Label(right, text="输出颜色", style="Section.TLabel").pack(anchor="w", padx=18)
        color_row = ttk.Frame(right, style="Panel.TFrame")
        color_row.pack(fill=tk.X, padx=18, pady=(2, 0))
        self.color_var = tk.StringVar(value=self.args.color)
        for key, label, style_name in (
            ("white", "白", "White"), ("red", "红", "Red"), ("blue", "蓝", "Blue"),
        ):
            ttk.Radiobutton(
                color_row, text=label, value=key, variable=self.color_var,
                style=f"{style_name}.TRadiobutton",
                command=self._on_color_change,
            ).pack(side=tk.LEFT, padx=(0, 16))
        self.swatch = tk.Canvas(color_row, width=18, height=18,
                                bg=self._swatch_hex(), highlightthickness=1,
                                highlightbackground=BORDER)
        self.swatch.pack(side=tk.RIGHT)

        self.binarize_var = tk.BooleanVar(value=not args_no_binarize(self.args))
        self.binarize_check = ttk.Checkbutton(
            right, text="二值化输出（亮 → 纯色，暗 → 黑）",
            variable=self.binarize_var, style="TCheckbutton",
            command=self._on_binarize_change,
        )
        self.binarize_check.pack(anchor="w", padx=18, pady=(7, 2))

        thr = ttk.Frame(right, style="Panel.TFrame")
        thr.pack(fill=tk.X, padx=18, pady=(3, 0))
        thr.columnconfigure(0, weight=1)
        self.thr_label = ttk.Label(thr, text="阈值", style="Panel.TLabel")
        self.thr_label.grid(row=0, column=0, sticky="w")
        self.threshold_var = tk.DoubleVar(value=float(self.args.threshold))
        self.thr_value = ttk.Label(thr, text=str(self.args.threshold), style="Value.TLabel")
        self.thr_value.grid(row=0, column=0, sticky="e")
        self.thr_scale = ttk.Scale(
            thr, from_=0, to=255, orient=tk.HORIZONTAL,
            variable=self.threshold_var, style="Horizontal.TScale",
        )
        self.thr_scale.grid(row=1, column=0, sticky="ew", pady=(2, 0))
        self.thr_auto = ttk.Button(thr, text="自动", style="Small.TButton",
                                   width=5, command=self.auto_threshold)
        self.thr_auto.grid(row=1, column=1, padx=(8, 0))
        ttk.Label(
            right, text="亮度 ≥ 阈值 → 输出颜色；其余保持黑色",
            style="Hint.TLabel", wraplength=324,
        ).pack(anchor="w", padx=18, pady=(3, 0))

        self._divider(right, pady=(9, 6))

        # ------------------------------------------------------------ 画笔
        ttk.Label(right, text="画笔（在 64×64 预览上拖动）",
                  style="Section.TLabel").pack(anchor="w", padx=18)
        paint_row = ttk.Frame(right, style="Panel.TFrame")
        paint_row.pack(fill=tk.X, padx=18, pady=(2, 0))
        self.paint_mode_var = tk.BooleanVar(value=False)
        self.paint_check = ttk.Checkbutton(
            paint_row, text="画笔模式", variable=self.paint_mode_var,
            style="TCheckbutton", command=self._on_paint_mode,
        )
        self.paint_check.pack(side=tk.LEFT)

        self.brush_buttons = {}
        for key in BRUSH_ORDER:
            label, bg, fg = BRUSH_COLORS[key]
            btn = tk.Button(
                paint_row, text=label, width=1, height=1,
                bg=bg, fg=fg, activebackground=bg, activeforeground=fg,
                relief="raised", bd=1, highlightthickness=0,
                padx=4, pady=1, font=(self.family, 9),
                command=lambda k=key: self._set_brush_color(k),
            )
            btn.pack(side=tk.LEFT, padx=(5, 0))
            self.brush_buttons[key] = btn

        self.btn_clear_paint = ttk.Button(
            paint_row, text="清除", style="Small.TButton", width=4,
            command=self.clear_paint)
        self.btn_clear_paint.pack(side=tk.RIGHT)
        self.btn_undo_paint = ttk.Button(
            paint_row, text="撤销", style="Small.TButton", width=4,
            command=self.undo_paint)
        self.btn_undo_paint.pack(side=tk.RIGHT, padx=(0, 4))
        ttk.Label(
            right, text="每格 = 选区 1/64；画笔模式下左图/预览均可绘制",
            style="Hint.TLabel", wraplength=324,
        ).pack(anchor="w", padx=18, pady=(3, 0))
        self._update_brush_buttons()

        self._divider(right, pady=(9, 6))

        # ------------------------------------------------------ 点阵屏参数
        ttk.Label(right, text="点阵屏参数", style="Section.TLabel").pack(anchor="w", padx=18)
        params = ttk.Frame(right, style="Panel.TFrame")
        params.pack(fill=tk.X, padx=18, pady=(3, 0))
        params.columnconfigure(0, weight=1)
        params.columnconfigure(1, weight=1)

        bcol = ttk.Frame(params, style="Panel.TFrame")
        bcol.grid(row=0, column=0, sticky="ew", padx=(0, 7))
        bcol.columnconfigure(0, weight=1)
        self.bright_label = ttk.Label(bcol, text="亮度", style="Panel.TLabel")
        self.bright_label.grid(row=0, column=0, sticky="w")
        self.brightness_var = tk.DoubleVar(value=float(self.args.brightness))
        self.bright_value = ttk.Label(bcol, text=str(int(self.args.brightness)),
                                      style="Value.TLabel")
        self.bright_value.grid(row=0, column=0, sticky="e")
        ttk.Scale(bcol, from_=0, to=100, orient=tk.HORIZONTAL,
                  variable=self.brightness_var,
                  style="Horizontal.TScale").grid(row=1, column=0, sticky="ew", pady=(2, 0))

        pcol = ttk.Frame(params, style="Panel.TFrame")
        pcol.grid(row=0, column=1, sticky="ew", padx=(7, 0))
        pcol.columnconfigure(0, weight=1)
        self.pwm_label = ttk.Label(pcol, text="PWM 位深", style="Panel.TLabel")
        self.pwm_label.grid(row=0, column=0, sticky="w")
        self.pwm_var = tk.DoubleVar(value=float(self.pwm_bits))
        self.pwm_value = ttk.Label(pcol, text=str(self.pwm_bits), style="Value.TLabel")
        self.pwm_value.grid(row=0, column=0, sticky="e")
        ttk.Scale(pcol, from_=1, to=11, orient=tk.HORIZONTAL,
                  variable=self.pwm_var,
                  style="Horizontal.TScale").grid(row=1, column=0, sticky="ew", pady=(2, 0))

        self.luminance_var = tk.BooleanVar(value=self.luminance_correct)
        self.lum_check = ttk.Checkbutton(
            params, text="CIE 亮度校正", variable=self.luminance_var,
            style="TCheckbutton", command=self._on_luminance_change,
        )
        self.lum_check.grid(row=1, column=0, columnspan=2, sticky="w", pady=(7, 0))

        self._divider(right, pady=(9, 6))

        # ------------------------------------------------------------ 按钮
        btns = ttk.Frame(right, style="Panel.TFrame")
        btns.pack(fill=tk.X, padx=18)
        btns.columnconfigure(0, weight=1)
        btns.columnconfigure(1, weight=1)
        ttk.Button(btns, text="打开图片…", command=self.open_dialog).grid(
            row=0, column=0, sticky="ew", padx=(0, 4), pady=2)
        ttk.Button(btns, text="重新居中", command=self.reset_selection).grid(
            row=0, column=1, sticky="ew", padx=(4, 0), pady=2)
        ttk.Button(btns, text="旋转 90°", command=self.rotate_90).grid(
            row=1, column=0, sticky="ew", padx=(0, 4), pady=2)
        ttk.Button(btns, text="保存 64×64 PNG", command=self.save_crop).grid(
            row=1, column=1, sticky="ew", padx=(4, 0), pady=2)
        ttk.Button(btns, text="发送到点阵屏  (Enter)", style="Accent.TButton",
                   command=self.send_now).grid(
            row=2, column=0, sticky="ew", padx=(0, 4), pady=(6, 2))
        ttk.Button(btns, text="退出", command=self.close).grid(
            row=2, column=1, sticky="ew", padx=(4, 0), pady=(6, 2))

        # 变量变化统一走 trace，拖动时数值标签与预览同步
        self.threshold_var.trace_add("write", self._on_threshold_change)
        self.brightness_var.trace_add("write", self._on_brightness_change)
        self.pwm_var.trace_add("write", self._on_pwm_change)
        self._apply_binarize_state()
        self._install_tooltips()

    def _install_tooltips(self):
        self._tooltips.append(Tooltip(
            self.binarize_check,
            "二值化：亮度 ≥ 阈值 → 输出颜色，其余 → 黑色。适合点阵屏的纯色显示。",
            self.family))
        self._tooltips.append(Tooltip(
            self.thr_label,
            "阈值（0–255）：亮于此值的像素变成输出颜色，暗的保持黑色。\n"
            "点「自动」可用 Otsu 算法估算。", self.family))
        self._tooltips.append(Tooltip(
            self.paint_check,
            "画笔模式：在左侧选区或右侧 64×64 预览上按住左键拖动绘制。\n"
            "每一格 = 当前选区的 1/64；关闭画笔后可继续调整选区。", self.family))
        for key in BRUSH_ORDER:
            label, _bg, _fg = BRUSH_COLORS[key]
            self._tooltips.append(Tooltip(
                self.brush_buttons[key],
                f"画笔颜色：{label}。最小单位 = 当前选区的 1/64。\n"
                "黑色可用于覆盖不想要的亮像素。", self.family))
        self._tooltips.append(Tooltip(
            self.btn_undo_paint, "撤销上一笔画笔覆盖。", self.family))
        self._tooltips.append(Tooltip(
            self.btn_clear_paint, "清空全部画笔覆盖。", self.family))
        self._tooltips.append(Tooltip(
            self.bright_label,
            "点阵屏整体亮度（0–100）。越低越省电、越不刺眼。", self.family))
        self._tooltips.append(Tooltip(
            self.pwm_label,
            "PWM 位深（1–11）：每个颜色通道的灰度位数。\n"
            "位深越低刷新率越高、闪烁越少，但灰阶越少；\n"
            "纯色二值图建议 3–6。", self.family))
        self._tooltips.append(Tooltip(
            self.lum_check,
            "CIE1931 亮度校正：按人眼感知重新映射亮度。\n"
            "关闭后亮度变化更线性；纯色二值图影响不大。", self.family))
        self._tooltips.append(Tooltip(
            self.live_check,
            "勾选后拖动选区/修改参数会立即更新点阵屏；\n"
            "取消后需按 Enter 或「发送到点阵屏」才更新。", self.family))

    def _swatch_hex(self):
        r, g, b = COLOR_CHOICES[self.color_var.get()]
        return f"#{r:02x}{g:02x}{b:02x}"

    def _bind_keys(self):
        for seq, dx, dy, step in (
            ("<Left>", -1, 0, 1), ("<Right>", 1, 0, 1),
            ("<Up>", 0, -1, 1), ("<Down>", 0, 1, 1),
            ("<Shift-Left>", -1, 0, 10), ("<Shift-Right>", 1, 0, 10),
            ("<Shift-Up>", 0, -1, 10), ("<Shift-Down>", 0, 1, 10),
        ):
            self.root.bind(seq, lambda e, dx=dx, dy=dy, step=step: self._nudge(dx, dy, step))
        for seq, delta in (
            ("<plus>", 1), ("<equal>", 1), ("<KP_Add>", 1),
            ("<minus>", -1), ("<underscore>", -1), ("<KP_Subtract>", -1),
            ("<Shift-plus>", 10), ("<Shift-equal>", 10), ("<Shift-minus>", -10),
            ("<Shift-underscore>", -10),
        ):
            self.root.bind(seq, lambda e, delta=delta: self._resize(delta))
        self.root.bind("<Return>", lambda e: self.send_now())
        self.root.bind("<Escape>", lambda e: self.close())
        self.root.bind("<Control-o>", lambda e: self.open_dialog())
        self.root.bind("<KeyPress-r>", lambda e: self.reset_selection())
        self.root.bind("<KeyPress-R>", lambda e: self.reset_selection())
        self.root.bind("<KeyPress-c>", lambda e: self.cycle_color())
        self.root.bind("<KeyPress-C>", lambda e: self.cycle_color())
        self.root.bind("<KeyPress-t>", lambda e: self.toggle_binarize())
        self.root.bind("<KeyPress-T>", lambda e: self.toggle_binarize())
        self.root.bind("<Control-r>", lambda e: self.rotate_90())
        self.root.bind("<Control-R>", lambda e: self.rotate_90())
        self.root.bind("<KeyPress-p>", lambda e: self.toggle_paint_mode())
        self.root.bind("<KeyPress-P>", lambda e: self.toggle_paint_mode())
        for key, color in zip(("1", "2", "3", "4"), BRUSH_ORDER):
            self.root.bind(f"<KeyPress-{key}>",
                           lambda e, c=color: self._set_brush_color(c))

    # --------------------------------------------------------------- start
    def _startup(self):
        if self.args.image:
            self.load_image(os.path.expanduser(self.args.image), self.args.region)
        else:
            self.open_dialog()
        if self.image is None:
            self._show_placeholder()

    def _show_placeholder(self):
        w = max(self.canvas.winfo_width(), 500)
        h = max(self.canvas.winfo_height(), 400)
        self.canvas.delete("all")
        self.canvas.create_text(
            w // 2, h // 2, text="请打开一张图片\n（右侧「打开图片…」）",
            fill=MUTED, font=(self.family, 16), justify="center",
        )

    # ---------------------------------------------------------------- image
    def open_dialog(self):
        initialdir = os.path.dirname(self.image_path) if self.image_path else os.path.expanduser("~/Pictures")
        if not os.path.isdir(initialdir):
            initialdir = os.path.expanduser("~")
        path = filedialog.askopenfilename(
            parent=self.root, title="选择图片", initialdir=initialdir,
            filetypes=[("图片文件", "*.png *.jpg *.jpeg *.bmp *.gif *.webp *.tif *.tiff"),
                       ("所有文件", "*.*")],
        )
        if path:
            self.load_image(path)

    def load_image(self, path, region=None):
        path = os.path.abspath(os.path.expanduser(path))
        try:
            with Image.open(path) as im:
                img = to_rgb(im)
        except FileNotFoundError:
            messagebox.showerror("打开失败", f"找不到图片：\n{path}", parent=self.root)
            return
        except Exception as exc:
            messagebox.showerror("打开失败", f"无法读取图片：\n{path}\n\n{exc}", parent=self.root)
            return

        self.image = img
        self.image_path = path
        self.drag_mode = None
        self._error_shown = False
        # 换图时清空画笔，避免旧覆盖残留
        self.paint = {}
        self._paint_undo.clear()
        self._pending_undo = None
        self._last_paint_cell = None
        self.base64 = None
        self.min_size = max(1, min(MIN_SELECTION, img.width, img.height))
        size = min(img.width, img.height)
        self.sel_size = size
        self.sel_x = (img.width - size) // 2
        self.sel_y = (img.height - size) // 2

        if region is not None:
            x, y, size = region
            size = int(clamp(size, self.min_size, min(img.width, img.height)))
            x = int(clamp(x, 0, img.width - size))
            y = int(clamp(y, 0, img.height - size))
            self.sel_x, self.sel_y, self.sel_size = x, y, size

        self.root.title(f"点阵屏图片选区 — {os.path.basename(path)}")
        self._dirty = False
        self.canvas.update_idletasks()
        self.redraw()
        self._update_crop(send=True)
        self.canvas.focus_set()
        print(f"已打开：{path}（{img.width}x{img.height}），选区 {self.sel_size}px")

    # ---------------------------------------------------------------- draw
    def redraw(self):
        """重建整张画布（打开图片 / 窗口尺寸变化时调用）。"""
        c = self.canvas
        c.delete("all")
        self._sel_item = None
        self._handle_items = []
        self._shade_items = []
        self._paint_items = {}
        self._label_item = None
        self._label_shadow = None
        self._img_item = None

        if self.image is None:
            self._show_placeholder()
            return

        cw, ch = c.winfo_width(), c.winfo_height()
        if cw < 50 or ch < 50:
            self.root.after(100, self.redraw)
            return

        w, h = self.image.size
        margin = 16
        scale = min((cw - margin * 2) / w, (ch - margin * 2) / h)
        scale = max(scale, 0.0001)
        dw = max(1, int(round(w * scale)))
        dh = max(1, int(round(h * scale)))
        self.disp_scale = scale
        self.disp_w, self.disp_h = dw, dh
        self.disp_x = (cw - dw) // 2
        self.disp_y = (ch - dh) // 2

        resample = Image.Resampling.NEAREST if scale >= 2 else Image.Resampling.LANCZOS
        disp = self.image.resize((dw, dh), resample)
        self.canvas_photo = photo_from_pil(self.root, disp)
        self._img_item = c.create_image(self.disp_x, self.disp_y, anchor="nw",
                                        image=self.canvas_photo)

        # 选区之外压暗（四个矩形，之后只改坐标）
        for _ in range(4):
            self._shade_items.append(
                c.create_rectangle(0, 0, 0, 0, fill="#000000", stipple="gray50",
                                   outline="", state="hidden"))
        self._sel_item = c.create_rectangle(
            0, 0, 0, 0, outline=ACCENT, width=2, state="hidden")
        for _ in range(4):
            self._handle_items.append(
                c.create_rectangle(0, 0, 0, 0, fill=ACCENT, outline="#0b3d22",
                                   width=1, state="hidden"))
        self._label_shadow = c.create_text(
            0, 0, anchor="nw", text="", fill="#000000",
            font=(self.family, 9, "bold"), state="hidden")
        self._label_item = c.create_text(
            0, 0, anchor="nw", text="", fill="#ffffff",
            font=(self.family, 9, "bold"), state="hidden")

        self._update_overlay()
        self._rebuild_paint_overlay()

    def _update_overlay(self):
        """只更新选区覆盖层坐标，不重画底图（拖动顺滑的关键）。"""
        if self.image is None or self._sel_item is None:
            return
        c = self.canvas
        sx, sy, ss = self._selection_display_rect()

        left, top = self.disp_x, self.disp_y
        right, bottom = left + self.disp_w, top + self.disp_h
        rects = (
            (left, top, right, sy),
            (left, sy + ss, right, bottom),
            (left, sy, sx, sy + ss),
            (sx + ss, sy, right, sy + ss),
        )
        for item, (x0, y0, x1, y1) in zip(self._shade_items, rects):
            if x1 > x0 and y1 > y0:
                c.coords(item, x0, y0, x1, y1)
                c.itemconfigure(item, state="normal")
            else:
                c.itemconfigure(item, state="hidden")

        c.coords(self._sel_item, sx, sy, sx + ss, sy + ss)
        c.itemconfigure(self._sel_item, state="normal")

        half = HANDLE_HALF
        corners = ((sx, sy), (sx + ss, sy), (sx, sy + ss), (sx + ss, sy + ss))
        for item, (hx, hy) in zip(self._handle_items, corners):
            c.coords(item, hx - half, hy - half, hx + half, hy + half)
            c.itemconfigure(item, state="normal")

        text = f"{self.sel_size}×{self.sel_size}"
        if ss >= 34:
            tx, ty = sx + 7, sy + 5
            c.coords(self._label_item, tx, ty)
            c.itemconfigure(self._label_item, text=text, state="normal")
            c.coords(self._label_shadow, tx + 1, ty + 1)
            c.itemconfigure(self._label_shadow, text=text, state="normal")
        else:
            c.itemconfigure(self._label_item, state="hidden")
            c.itemconfigure(self._label_shadow, state="hidden")

    # -------------------------------------------------------------- cropping
    def _small_rgb(self, crop):
        return downscale_square(crop, MATRIX_SIZE)

    def _process_crop(self, crop):
        """返回 64×64 RGB：可选的二值化 + 纯色着色 + 90° 旋转。"""
        small = self._small_rgb(crop)
        if self.binarize_var.get():
            gray = small.convert("L")
            t = int(round(self.threshold_var.get()))
            lut = [255 if i >= t else 0 for i in range(256)]
            mask = gray.point(lut).convert("1")
            color = Image.new("RGB", (MATRIX_SIZE, MATRIX_SIZE),
                              COLOR_CHOICES[self.color_var.get()])
            out = Image.new("RGB", (MATRIX_SIZE, MATRIX_SIZE), (0, 0, 0))
            out.paste(color, (0, 0), mask)
        else:
            out = small
        # PIL 的 ROTATE_xxx 是逆时针；这里按顺时针 90° 递增
        transpose = {
            90: Image.Transpose.ROTATE_270,
            180: Image.Transpose.ROTATE_180,
            270: Image.Transpose.ROTATE_90,
        }.get(self.rotation)
        if transpose is not None:
            out = out.transpose(transpose)
        return out

    def _update_crop(self, send=None):
        if self.image is None:
            return
        size = self.sel_size
        x = int(clamp(self.sel_x, 0, self.image.width - size))
        y = int(clamp(self.sel_y, 0, self.image.height - size))
        if (x, y) != (self.sel_x, self.sel_y):
            self.sel_x, self.sel_y = x, y
            self._update_overlay()
            self._rebuild_paint_overlay()

        crop = self.image.crop((x, y, x + size, y + size))
        self.base64 = self._process_crop(crop)
        self._render_output(send=send)

        if send is None:
            send = self.live_var.get()
        if send and os.environ.get("CROP_SELECTOR_DEBUG"):
            print(f"DEBUG: send ({x},{y}) {size}x{size} "
                  f"binarize={self.binarize_var.get()} "
                  f"color={self.color_var.get()} "
                  f"threshold={int(round(self.threshold_var.get()))} "
                  f"paint={len(self.paint)} "
                  f"brightness={self.matrix.brightness}", flush=True)

    def _render_output(self, send=None):
        """在 base64 上叠加画笔覆盖，生成最终 64×64 并刷新预览。"""
        if self.base64 is None:
            return
        out = self.base64.copy()
        if self.paint:
            px = out.load()
            for (col, row), key in self.paint.items():
                if 0 <= col < MATRIX_SIZE and 0 <= row < MATRIX_SIZE:
                    px[col, row] = PAINT_COLORS[key]
        self.crop64 = out

        preview = out.resize((PREVIEW_PIXELS, PREVIEW_PIXELS),
                             Image.Resampling.NEAREST)
        self.preview_photo = photo_from_pil(self.root, preview)
        self.preview_canvas.delete("all")
        self.preview_canvas.create_image(PREVIEW_PAD, PREVIEW_PAD, anchor="nw",
                                         image=self.preview_photo)

        self._refresh_info()

        if send is None:
            send = self.live_var.get()
        if send and self.matrix is not None:
            self._send_to_matrix(out)

    def _refresh_info(self):
        if self.image is None:
            return
        mode = "二值化" if self.binarize_var.get() else "原始彩色"
        if self.binarize_var.get():
            mode += f" · {COLOR_NAMES[self.color_var.get()]}色"
        if self.rotation:
            mode += f" · 旋转 {self.rotation}°"
        self.info_var.set(
            f"原图 {self.image.width}×{self.image.height}\n"
            f"选区 {self.sel_size}×{self.sel_size} @ ({self.sel_x}, {self.sel_y})\n"
            f"输出 64×64 · {mode}"
        )

    def _send_to_matrix(self, img):
        """直接写活动帧。

        注意：本机 RP1 PIO 后端下 CreateFrameCanvas()/SwapOnVSync() 会在
        第一次交换时死锁（主线程卡在 futex，刷屏线程卡在
        rp1_pio_sm_xfer_data32_user）。所以不要用双缓冲，保持单帧直写。
        unsafe=False 是项目里验证过的路径，64×64 只有 4096 像素，开销可接受。
        """
        try:
            self.matrix.SetImage(img, unsafe=False)
        except Exception as exc:
            if not self._error_shown:
                self._error_shown = True
                messagebox.showerror("显示失败", str(exc), parent=self.root)

    def _tick(self):
        if self._closing:
            return
        if self._dirty:
            self._dirty = False
            self._resend = False
            self._update_crop()
        elif self._resend:
            # 亮度 / PWM / 画笔变化后重发当前帧，保证点阵屏立即刷新
            self._resend = False
            if self.crop64 is not None and self.matrix is not None:
                self._send_to_matrix(self.crop64)
        self._tick_id = self.root.after(TICK_MS, self._tick)

    def _schedule_tick(self):
        self._tick_id = self.root.after(TICK_MS, self._tick)

    # ---------------------------------------------------------------- mouse
    def _selection_display_rect(self):
        ss = self.sel_size * self.disp_scale
        sx = self.disp_x + self.sel_x * self.disp_scale
        sy = self.disp_y + self.sel_y * self.disp_scale
        return sx, sy, ss

    def _hit_mode(self, x, y):
        if self.image is None:
            return None
        sx, sy, ss = self._selection_display_rect()
        corners = {
            "nw": (sx, sy), "ne": (sx + ss, sy),
            "sw": (sx, sy + ss), "se": (sx + ss, sy + ss),
        }
        best, best_dist = None, HANDLE_HIT
        for name, (cx, cy) in corners.items():
            dist = max(abs(x - cx), abs(y - cy))
            if dist <= best_dist:
                best, best_dist = name, dist
        if best:
            return best
        if sx <= x <= sx + ss and sy <= y <= sy + ss:
            return "move"
        return None

    def _set_cursor_for(self, x, y):
        mode = self._hit_mode(x, y)
        if mode in ("nw", "ne", "sw", "se"):
            cursor = "crosshair"
        elif mode == "move":
            cursor = "fleur"
        else:
            cursor = ""
        if cursor != self._cursor:
            self._cursor = cursor
            try:
                self.canvas.configure(cursor=cursor)
            except tk.TclError:
                pass

    def _on_press(self, event):
        if self.image is None:
            return
        if self.paint_mode_var.get():
            cell = self._canvas_to_output_cell(event.x, event.y)
            if cell is not None:
                self._pending_undo = dict(self.paint)
                self._last_paint_cell = None
                self._paint_stroke_to(cell)
            return
        mode = self._hit_mode(event.x, event.y)
        if mode is None:
            self.drag_mode = None
            return
        self.drag_mode = mode
        self.drag_start = (event.x, event.y)
        self.drag_sel = (self.sel_x, self.sel_y, self.sel_size)
        self._clear_paint_overlay_items()   # 拖动选区时先隐藏画笔覆盖
        self._set_cursor_for(event.x, event.y)

    def _on_drag(self, event):
        if self.image is None:
            return
        if self.paint_mode_var.get():
            cell = self._canvas_to_output_cell(event.x, event.y)
            if cell is not None:
                self._paint_stroke_to(cell)
            return
        if self.drag_mode is None:
            return
        img_w, img_h = self.image.size
        x0, y0, s0 = self.drag_sel
        if self.drag_mode == "move":
            du = (event.x - self.drag_start[0]) / self.disp_scale
            dv = (event.y - self.drag_start[1]) / self.disp_scale
            new_x = int(round(clamp(x0 + du, 0, img_w - s0)))
            new_y = int(round(clamp(y0 + dv, 0, img_h - s0)))
            new = (new_x, new_y, s0)
        else:
            if self.drag_mode == "nw":
                fx, fy = x0 + s0, y0 + s0
            elif self.drag_mode == "ne":
                fx, fy = x0, y0 + s0
            elif self.drag_mode == "sw":
                fx, fy = x0 + s0, y0
            else:  # se
                fx, fy = x0, y0

            u = clamp((event.x - self.disp_x) / self.disp_scale, 0, img_w)
            v = clamp((event.y - self.disp_y) / self.disp_scale, 0, img_h)
            size = int(round(max(abs(u - fx), abs(v - fy))))

            max_w = fx if self.drag_mode in ("nw", "sw") else img_w - fx
            max_h = fy if self.drag_mode in ("nw", "ne") else img_h - fy
            max_size = min(max_w, max_h)
            if max_size < self.min_size:
                return
            size = int(clamp(size, self.min_size, max_size))

            if self.drag_mode == "nw":
                new_x, new_y = fx - size, fy - size
            elif self.drag_mode == "ne":
                new_x, new_y = fx, fy - size
            elif self.drag_mode == "sw":
                new_x, new_y = fx - size, fy
            else:
                new_x, new_y = fx, fy
            new = (new_x, new_y, size)

        if new != (self.sel_x, self.sel_y, self.sel_size):
            self.sel_x, self.sel_y, self.sel_size = new
            self._dirty = True
            self._update_overlay()     # 只动覆盖层，不再整张重画

    def _on_release(self, event):
        if self.paint_mode_var.get():
            self._last_paint_cell = None
            self._pending_undo = None
            return
        self.drag_mode = None
        self._set_cursor_for(event.x, event.y)
        self._rebuild_paint_overlay()       # 选区位置确定后恢复画笔覆盖

    def _on_motion(self, event):
        if self.paint_mode_var.get():
            return
        if self.drag_mode is None:
            self._set_cursor_for(event.x, event.y)

    def _on_configure(self, event):
        if self.image is None:
            return
        if self._resize_id is not None:
            try:
                self.root.after_cancel(self._resize_id)
            except tk.TclError:
                pass
        self._resize_id = self.root.after(120, self._after_resize)

    def _after_resize(self):
        self._resize_id = None
        self.redraw()

    # -------------------------------------------------------------- keyboard
    def _nudge(self, dx, dy, step):
        if self.image is None:
            return
        size = self.sel_size
        nx = int(clamp(self.sel_x + dx * step, 0, self.image.width - size))
        ny = int(clamp(self.sel_y + dy * step, 0, self.image.height - size))
        if (nx, ny) != (self.sel_x, self.sel_y):
            self.sel_x, self.sel_y = nx, ny
            self._dirty = True
            self._update_overlay()
            self._rebuild_paint_overlay()

    def _resize(self, delta):
        if self.image is None:
            return
        max_size = min(self.image.size)
        new_size = int(clamp(self.sel_size + delta, self.min_size, max_size))
        if new_size == self.sel_size:
            return
        cx = self.sel_x + self.sel_size / 2.0
        cy = self.sel_y + self.sel_size / 2.0
        self.sel_size = new_size
        self.sel_x = int(round(clamp(cx - new_size / 2.0, 0, self.image.width - new_size)))
        self.sel_y = int(round(clamp(cy - new_size / 2.0, 0, self.image.height - new_size)))
        self._dirty = True
        self._update_overlay()
        self._rebuild_paint_overlay()

    def reset_selection(self):
        if self.image is None:
            return
        size = min(self.image.size)
        self.sel_size = size
        self.sel_x = (self.image.width - size) // 2
        self.sel_y = (self.image.height - size) // 2
        self._dirty = True
        self._update_overlay()
        self._rebuild_paint_overlay()

    # ---------------------------------------------------------------- actions
    def _on_live_toggle(self):
        if self.live_var.get():
            self._update_crop(send=True)

    def _on_color_change(self):
        try:
            self.swatch.configure(bg=self._swatch_hex())
        except tk.TclError:
            pass
        self._dirty = True
        self._refresh_info()

    def cycle_color(self):
        i = COLOR_ORDER.index(self.color_var.get())
        self.color_var.set(COLOR_ORDER[(i + 1) % len(COLOR_ORDER)])
        self._on_color_change()

    def toggle_binarize(self):
        self.binarize_var.set(not self.binarize_var.get())
        self._on_binarize_change()

    def rotate_90(self):
        self.rotation = (self.rotation + 90) % 360
        if self.paint:
            # 画笔覆盖跟随输出一起顺时针旋转，避免转完后覆盖错位
            n = MATRIX_SIZE - 1
            self.paint = {(n - row, col): key
                          for (col, row), key in self.paint.items()}
            self._paint_undo.clear()
        self._pending_undo = None
        self._last_paint_cell = None
        self._rebuild_paint_overlay()
        self._dirty = True
        self._refresh_info()
        print(f"输出旋转：{self.rotation}°", flush=True)

    def _on_binarize_change(self):
        self._apply_binarize_state()
        self._dirty = True
        self._refresh_info()

    def _apply_binarize_state(self):
        on = self.binarize_var.get()
        state = "normal" if on else "disabled"
        try:
            self.thr_scale.configure(state=state)
            self.thr_auto.configure(state=state)
        except tk.TclError:
            pass

    def _on_threshold_change(self, *args):
        try:
            value = int(round(self.threshold_var.get()))
        except (tk.TclError, ValueError):
            return
        value = int(clamp(value, 0, 255))
        self.thr_value.configure(text=str(value))
        self._dirty = True

    def _on_brightness_change(self, *args):
        try:
            value = int(round(self.brightness_var.get()))
        except (tk.TclError, ValueError):
            return
        value = int(clamp(value, 0, 100))
        self.bright_value.configure(text=str(value))
        if self.matrix is not None:
            try:
                self.matrix.brightness = value
                self._resend = True   # 下一 tick 重发当前帧
            except Exception:
                pass

    def _on_pwm_change(self, *args):
        try:
            value = int(round(self.pwm_var.get()))
        except (tk.TclError, ValueError):
            return
        value = int(clamp(value, 1, 11))
        self.pwm_value.configure(text=str(value))
        self.pwm_bits = value
        self._apply_panel_params()
        self._resend = True

    def _on_luminance_change(self):
        self.luminance_correct = bool(self.luminance_var.get())
        self._apply_panel_params()
        self._resend = True

    def _apply_panel_params(self):
        """把 PWM 位深 / CIE 校正应用到当前活动帧。"""
        if self.matrix is None:
            return
        try:
            self.matrix.pwmBits = self.pwm_bits
            self.matrix.luminanceCorrect = self.luminance_correct
        except Exception:
            pass

    # ---------------------------------------------------------------- paint
    def _set_brush_color(self, key):
        if key not in BRUSH_COLORS:
            return
        self.brush_color = key
        self._update_brush_buttons()

    def _update_brush_buttons(self):
        for key, btn in getattr(self, "brush_buttons", {}).items():
            selected = key == self.brush_color
            try:
                btn.configure(relief="sunken" if selected else "raised",
                              bd=2 if selected else 1)
            except tk.TclError:
                pass

    def toggle_paint_mode(self):
        self.paint_mode_var.set(not self.paint_mode_var.get())
        self._on_paint_mode()

    def _on_paint_mode(self):
        on = self.paint_mode_var.get()
        cursor = "crosshair" if on else ""
        try:
            self.preview_card.configure(
                highlightbackground=ACCENT if on else BORDER)
            self.preview_canvas.configure(cursor=cursor)
            self.canvas.configure(cursor=cursor)
            self._cursor = cursor
            if on:
                self.drag_mode = None
                self._rebuild_paint_overlay()
        except tk.TclError:
            pass

    def _preview_to_cell(self, x, y):
        px = x - PREVIEW_PAD
        py = y - PREVIEW_PAD
        if not (0 <= px < PREVIEW_PIXELS and 0 <= py < PREVIEW_PIXELS):
            return None
        col = px * MATRIX_SIZE // PREVIEW_PIXELS
        row = py * MATRIX_SIZE // PREVIEW_PIXELS
        return (int(clamp(col, 0, MATRIX_SIZE - 1)),
                int(clamp(row, 0, MATRIX_SIZE - 1)))

    # 旋转映射：base = 旋转前的选区网格，output = 旋转后的 64×64 网格
    def _base_to_output_cell(self, col, row):
        n = MATRIX_SIZE - 1
        if self.rotation == 90:
            return n - row, col
        if self.rotation == 180:
            return n - col, n - row
        if self.rotation == 270:
            return row, n - col
        return col, row

    def _output_to_base_cell(self, col, row):
        n = MATRIX_SIZE - 1
        if self.rotation == 90:
            return row, n - col
        if self.rotation == 180:
            return n - col, n - row
        if self.rotation == 270:
            return n - row, col
        return col, row

    def _canvas_to_output_cell(self, x, y):
        if self.image is None or self.sel_size <= 0:
            return None
        ix = (x - self.disp_x) / self.disp_scale
        iy = (y - self.disp_y) / self.disp_scale
        if not (self.sel_x <= ix < self.sel_x + self.sel_size and
                self.sel_y <= iy < self.sel_y + self.sel_size):
            return None
        col = int((ix - self.sel_x) * MATRIX_SIZE / self.sel_size)
        row = int((iy - self.sel_y) * MATRIX_SIZE / self.sel_size)
        col = int(clamp(col, 0, MATRIX_SIZE - 1))
        row = int(clamp(row, 0, MATRIX_SIZE - 1))
        return self._base_to_output_cell(col, row)

    @staticmethod
    def _paint_hex(key):
        r, g, b = PAINT_COLORS[key]
        return f"#{r:02x}{g:02x}{b:02x}"

    def _draw_paint_cell(self, cell):
        key = self.paint.get(cell)
        if key is None:
            self._remove_paint_item(cell)
            return
        if self.image is None or self._sel_item is None:
            return
        col, row = cell
        base_col, base_row = self._output_to_base_cell(col, row)
        sx, sy, ss = self._selection_display_rect()
        cw = ss / MATRIX_SIZE
        x0 = int(round(sx + base_col * cw))
        y0 = int(round(sy + base_row * cw))
        x1 = int(round(sx + (base_col + 1) * cw))
        y1 = int(round(sy + (base_row + 1) * cw))
        if x1 <= x0:
            x1 = x0 + 1
        if y1 <= y0:
            y1 = y0 + 1
        item = self._paint_items.get(cell)
        if item is not None:
            try:
                self.canvas.coords(item, x0, y0, x1, y1)
                self.canvas.itemconfigure(item, fill=self._paint_hex(key))
                return
            except tk.TclError:
                self._paint_items.pop(cell, None)
        try:
            item = self.canvas.create_rectangle(
                x0, y0, x1, y1, fill=self._paint_hex(key),
                outline="", tags=("paint",))
            try:
                self.canvas.tag_lower(item, self._shade_items[0])
            except (tk.TclError, IndexError):
                pass
            self._paint_items[cell] = item
        except tk.TclError:
            pass

    def _remove_paint_item(self, cell):
        item = self._paint_items.pop(cell, None)
        if item is not None:
            try:
                self.canvas.delete(item)
            except tk.TclError:
                pass

    def _clear_paint_overlay_items(self):
        try:
            self.canvas.delete("paint")
        except tk.TclError:
            pass
        self._paint_items.clear()

    def _rebuild_paint_overlay(self):
        self._clear_paint_overlay_items()
        if self.image is None or self._sel_item is None or not self.paint:
            return
        for cell in self.paint:
            self._draw_paint_cell(cell)
        try:
            self.canvas.tag_lower("paint", self._shade_items[0])
        except (tk.TclError, IndexError):
            pass

    def _on_preview_press(self, event):
        if not self.paint_mode_var.get() or self.image is None:
            return
        cell = self._preview_to_cell(event.x, event.y)
        if cell is None:
            return
        self._pending_undo = dict(self.paint)   # 真正改到像素时才入撤销栈
        self._last_paint_cell = None
        self._paint_stroke_to(cell)

    def _on_preview_drag(self, event):
        if not self.paint_mode_var.get() or self.image is None:
            return
        cell = self._preview_to_cell(event.x, event.y)
        if cell is not None:
            self._paint_stroke_to(cell)

    def _on_preview_release(self, event):
        self._last_paint_cell = None
        self._pending_undo = None

    def _paint_stroke_to(self, cell):
        """把相邻的两个鼠标采样点连成线，快速拖动也不会漏格。"""
        changed_cells = []
        if self._last_paint_cell is None:
            if self._set_paint_cell(cell):
                changed_cells.append(cell)
        else:
            x0, y0 = self._last_paint_cell
            x1, y1 = cell
            dx = abs(x1 - x0)
            dy = -abs(y1 - y0)
            sx = 1 if x0 < x1 else -1
            sy = 1 if y0 < y1 else -1
            err = dx + dy
            while True:
                if self._set_paint_cell((x0, y0)):
                    changed_cells.append((x0, y0))
                if x0 == x1 and y0 == y1:
                    break
                e2 = 2 * err
                if e2 >= dy:
                    err += dy
                    x0 += sx
                if e2 <= dx:
                    err += dx
                    y0 += sy
        self._last_paint_cell = cell
        if changed_cells:
            self._render_paint_update(changed_cells)

    def _set_paint_cell(self, cell):
        col, row = cell
        if not (0 <= col < MATRIX_SIZE and 0 <= row < MATRIX_SIZE):
            return False
        if self.paint.get((col, row)) == self.brush_color:
            return False
        if self._pending_undo is not None:
            self._paint_undo.append(self._pending_undo)
            if len(self._paint_undo) > 40:
                del self._paint_undo[0]
            self._pending_undo = None
        self.paint[(col, row)] = self.brush_color
        return True

    def _render_paint_update(self, changed=None):
        self._render_output(send=False)
        if changed is None:
            self._rebuild_paint_overlay()
        else:
            for cell in changed:
                self._draw_paint_cell(cell)
        if self.live_var.get():
            self._resend = True

    def _push_paint_undo(self):
        self._paint_undo.append(dict(self.paint))
        if len(self._paint_undo) > 40:
            del self._paint_undo[0]

    def undo_paint(self):
        if not self._paint_undo:
            return
        self.paint = self._paint_undo.pop()
        self._pending_undo = None
        self._last_paint_cell = None
        self._render_paint_update()

    def clear_paint(self):
        if not self.paint:
            return
        self._push_paint_undo()
        self.paint = {}
        self._pending_undo = None
        self._last_paint_cell = None
        self._render_paint_update()

    def auto_threshold(self):
        if self.image is None:
            return
        size = self.sel_size
        x = int(clamp(self.sel_x, 0, self.image.width - size))
        y = int(clamp(self.sel_y, 0, self.image.height - size))
        crop = self.image.crop((x, y, x + size, y + size))
        gray = self._small_rgb(crop).convert("L")
        self.threshold_var.set(float(otsu_threshold(gray)))

    def send_now(self):
        if self.image is None:
            return
        self._update_crop(send=True)
        print("已发送当前选区到点阵屏")

    def save_crop(self):
        if self.image is None:
            return
        self._update_crop(send=False)
        if self.crop64 is None:
            return
        initialdir = os.path.dirname(self.image_path) if self.image_path else os.path.expanduser("~")
        initial = f"crop-64x64-{time.strftime('%Y%m%d-%H%M%S')}.png"
        path = filedialog.asksaveasfilename(
            parent=self.root, title="保存 64×64 图片",
            defaultextension=".png", initialfile=initial, initialdir=initialdir,
            filetypes=[("PNG 图片", "*.png"), ("所有文件", "*.*")],
        )
        if path:
            self.crop64.save(path)
            print(f"已保存：{path}")
            messagebox.showinfo("已保存", f"已保存 64×64 图片：\n{path}", parent=self.root)

    # ---------------------------------------------------------------- signals
    def _install_signals(self):
        signal.signal(signal.SIGINT, self._on_signal)
        signal.signal(signal.SIGTERM, self._on_signal)
        signal.signal(signal.SIGHUP, self._on_signal)
        signal.signal(signal.SIGTSTP, signal.SIG_IGN)
        self.root.after(100, self._poll_signals)

    def _on_signal(self, signum, frame):
        self._stop_requested = True

    def _poll_signals(self):
        if self._closing:
            return
        if self._stop_requested:
            self.close()
        else:
            self.root.after(100, self._poll_signals)

    # ------------------------------------------------------------------ close
    def close(self):
        if self._closing:
            return
        self._closing = True
        if self._tick_id is not None:
            try:
                self.root.after_cancel(self._tick_id)
            except tk.TclError:
                pass
        try:
            if self.matrix is not None:
                self.matrix.Clear()
                time.sleep(0.3)
        finally:
            print("已清屏并退出。", flush=True)
            sys.stdout.flush()
            sys.stderr.flush()
            os._exit(0)


def args_no_binarize(args):
    return bool(getattr(args, "no_binarize", False))


def main(argv=None):
    args = parse_args(argv)
    try:
        # className 会写入 WM_CLASS，供桌面启动器 StartupWMClass 匹配
        root = tk.Tk(className="CropSelector")
    except tk.TclError as exc:
        print(f"错误：无法打开图形界面（DISPLAY 是否可用？）：{exc}", file=sys.stderr)
        return 1

    root.withdraw()
    try:
        app = CropSelectorApp(root, args)
    except Exception as exc:
        traceback.print_exc()
        messagebox.showerror("启动失败", str(exc), parent=root)
        root.destroy()
        return 1

    # 上一次进程用 os._exit 打断 DMA 后，内核偶尔无法停止 dma2chan0。
    # 此时窗口和程序都正常，但刷新线程只发出第一帧黑色画面就永久卡住。
    # 在显示窗口前做一次中断计数存活检查；卡死则交给启动器自动重试。
    if not refresh_is_progressing():
        print("点阵屏 DMA 通道卡死，自动退出并重试……", file=sys.stderr, flush=True)
        try:
            app.matrix.Clear()
        except Exception:
            pass
        time.sleep(0.3)
        sys.stdout.flush()
        sys.stderr.flush()
        os._exit(EXIT_STUCK)

    root.deiconify()
    root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
