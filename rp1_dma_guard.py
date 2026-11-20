"""检测 RP1 PIO 刷新线程是否被卡死的 DMA 通道拖住。

背景：进程用 ``os._exit`` 退出时，刷新线程几乎总在
``rp1_pio_sm_xfer_data32_user`` 里等 DMA。内核偶尔无法停止 ``dma2chan0``
（dmesg 里是 ``failed to stop`` / ``is non-idle!``）。下一次启动时 PIO 程序、
状态机和 DMA 配置都返回成功，窗口也能正常打开，但刷新线程只送出第一帧
（启动时清屏的黑色画面）就永久卡住，点阵屏一直黑。

正常刷新时 ``dw_axi_dmac_platform`` 中断约 100–200 次/秒；卡死时该计数
完全不动。本模块用 /proc/interrupts 做一次低成本的存活检查，卡死时由
启动器自动重启一次（见 crop-selector / show-image 两个启动器）。
"""

import time

# 启动器约定的“刷新线程卡死、请自动重试”退出码
EXIT_STUCK = 75

_IRQ_NAME = "dw_axi_dmac_platform"


def dma_irq_count():
    """返回 RP1 DMA 控制器的累计中断次数；读不到时返回 None。"""
    try:
        with open("/proc/interrupts", "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                if _IRQ_NAME not in line:
                    continue
                total = 0
                # 形如 "139:  33495  0  0  0 rp1_irq_chip 40 Level dw_axi_dmac_platform"
                # 只累加 CPU 列（第一个非数字之后是中断控制器名，不能再加）。
                for token in line.split()[1:]:
                    if not token.isdigit():
                        break
                    total += int(token)
                return total
    except OSError:
        return None
    return None


def refresh_is_progressing(window=0.6, min_delta=5, verbose=False):
    """采样两次 DMA 中断计数，判断刷新线程是否还在推进。

    读不到 /proc/interrupts 时按“正常”处理，避免误伤其他平台。
    """
    first = dma_irq_count()
    if first is None:
        return True
    time.sleep(window)
    second = dma_irq_count()
    if second is None:
        return True
    if verbose:
        print(f"DMA IRQ: {first} -> {second}", flush=True)
    return (second - first) >= min_delta
