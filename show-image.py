#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""在 64x64 RGB LED 点阵屏上显示一张静态图片（Raspberry Pi 5 + RP1 PIO）。

硬件：Raspberry Pi 5 + 微雪 RGB-Matrix-P2-64x64（HUB75，1/32 扫）
后端：RP1 PIO（/dev/pio0），默认无需 sudo（pi 用户须在 gpio 组内）

用法：
    cd ~/RGB-Matrix-Px-xx/progress
    ./show-image -i ~/Pictures/photo.jpg
    ./show-image -i logo.png --mode fill -B 60

安全退出：
    Ctrl-C（SIGINT）或 SIGTERM -> 清屏并释放 /dev/pio0。
    !! 切勿按 Ctrl-Z：挂起会让 PIO 停止刷新，屏幕留下亮线并锁死硬件 !!
"""

import argparse
import os
import signal
import sys
import threading
import time
import traceback

try:
    from PIL import Image, ImageOps
except ImportError:
    sys.exit("错误：缺少 Pillow。请先运行：~/RGB-Matrix-Px-xx/progress/venv/bin/pip install pillow")

try:
    from rgbmatrix import RGBMatrix, RGBMatrixOptions
except ImportError:
    sys.exit(
        "错误：未找到 rgbmatrix Python 绑定。\n"
        "请先运行：~/RGB-Matrix-Px-xx/progress/build-rgbmatrix-binding.sh"
    )

try:
    from rp1_dma_guard import EXIT_STUCK, refresh_is_progressing
except ImportError:
    EXIT_STUCK = 75

    def refresh_is_progressing(*args, **kwargs):
        return True

MATRIX_ROWS = 64
MATRIX_COLS = 64


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="在 64x64 RGB 点阵屏上显示一张静态图片（RP1 PIO 后端）",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("image", nargs="?", help="图片路径（也可用 -i/--image 指定）")
    parser.add_argument("-i", "--image", dest="image_opt", metavar="PATH",
                        help="图片路径")
    parser.add_argument("-m", "--mode", choices=("fit", "fill", "stretch"),
                        default="fit",
                        help="fit=完整显示并留黑边；fill=裁剪铺满；stretch=拉伸铺满")
    parser.add_argument("-B", "--brightness", type=int, default=100,
                        metavar="0-100", help="屏幕亮度")
    parser.add_argument("--show-refresh", action="store_true",
                        help="周期性打印实际刷新率（调试用）")
    args = parser.parse_args(argv)

    args.image_path = args.image_opt or args.image
    if not args.image_path:
        parser.error("必须提供图片路径（位置参数或 -i/--image）")
    if not 0 <= args.brightness <= 100:
        parser.error("亮度必须在 0-100 之间")
    return args


def _to_rgb(im):
    """转成 RGB；透明背景合成到黑色上，并尊重 EXIF 方向。"""
    im = ImageOps.exif_transpose(im)
    if im.mode in ("RGBA", "LA") or (im.mode == "P" and "transparency" in im.info):
        rgba = im.convert("RGBA")
        background = Image.new("RGBA", rgba.size, (0, 0, 0, 255))
        return Image.alpha_composite(background, rgba).convert("RGB")
    return im.convert("RGB")


def prepare_image(path, mode):
    """读取并缩放为恰好 64x64 的 RGB 图像。返回 (image, 原始尺寸)。"""
    try:
        with Image.open(path) as im:
            original_size = im.size
            img = _to_rgb(im)
    except FileNotFoundError:
        sys.exit(f"错误：找不到图片文件：{path}")
    except OSError as exc:
        sys.exit(f"错误：无法读取图片 {path}：{exc}")

    size = (MATRIX_COLS, MATRIX_ROWS)
    if mode == "fill":
        img = ImageOps.fit(img, size, method=Image.Resampling.LANCZOS,
                           centering=(0.5, 0.5))
    elif mode == "stretch":
        img = img.resize(size, Image.Resampling.LANCZOS)
    else:  # fit：保持比例，不放大，居中留黑边
        img.thumbnail(size, Image.Resampling.LANCZOS)
        canvas = Image.new("RGB", size, (0, 0, 0))
        canvas.paste(img, ((size[0] - img.width) // 2,
                           (size[1] - img.height) // 2))
        img = canvas
    return img, original_size


def build_matrix(args):
    options = RGBMatrixOptions()
    options.rows = MATRIX_ROWS
    options.cols = MATRIX_COLS
    options.chain_length = 1
    options.parallel = 1
    options.hardware_mapping = "regular"
    options.disable_hardware_pulsing = True
    options.gpio_slowdown = 2
    options.rp1_rio = 0          # 0 = RP1 PIO 后端（默认）
    options.brightness = args.brightness
    options.show_refresh_rate = args.show_refresh
    return RGBMatrix(options=options)


def main(argv=None):
    args = parse_args(argv)
    path = os.path.expanduser(args.image_path)

    stop = threading.Event()

    def request_stop(signum, frame):
        stop.set()

    for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        signal.signal(sig, request_stop)
    # 忽略 Ctrl-Z：挂起会停止 PIO 刷新，锁死 /dev/pio0 并留下亮线
    signal.signal(signal.SIGTSTP, signal.SIG_IGN)

    img, original_size = prepare_image(path, args.mode)
    if stop.is_set():
        print("已取消。")
        return 0

    print(f"图片：{path}（{original_size[0]}x{original_size[1]}）")
    print(f"模式：{args.mode}，亮度：{args.brightness}")
    print("正在初始化点阵屏……")
    matrix = build_matrix(args)

    # 上一次进程用 os._exit 打断 DMA 后，内核偶尔无法停止 dma2chan0。
    # 此时初始化全部成功，但刷新线程只发出第一帧就永久卡住（屏幕黑）。
    # 交给启动器自动重试，避免用户看到“程序在跑但屏是黑的”。
    if not refresh_is_progressing():
        print("点阵屏 DMA 通道卡死，自动退出并重试……", file=sys.stderr, flush=True)
        sys.stdout.flush()
        sys.stderr.flush()
        os._exit(EXIT_STUCK)

    print(f"点阵屏：{matrix.width}x{matrix.height}，"
          "按 Ctrl-C 退出（切勿按 Ctrl-Z）")

    exit_code = 0
    try:
        # unsafe=False：逐像素拷贝，4096 像素对静态图完全够用，
        # 且不依赖 Pillow 内部结构，兼容性最好。
        matrix.SetImage(img, unsafe=False)
        while not stop.wait(0.5):
            pass
    except BaseException:
        traceback.print_exc()
        exit_code = 1
    finally:
        # 注意：不要 del matrix / 让解释器析构 RGBMatrix。
        # 实测该库的 PIO 后端在停止刷新线程时偶发死锁（卡在析构里）。
        # 先清屏并等待约 30 帧，再直接 _exit：内核关闭 /dev/pio0
        # 时会释放硬件；实测屏幕保持全黑，之后可再次初始化。
        try:
            matrix.Clear()
        except BaseException:
            pass
        time.sleep(0.3)
        print("已清屏并安全退出。", flush=True)
        sys.stdout.flush()
        sys.stderr.flush()
        os._exit(exit_code)


if __name__ == "__main__":
    sys.exit(main())
