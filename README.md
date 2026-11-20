# RM_Armor_Identification

RoboMaster 装甲板识别调试用的**可控显示端**：在 Raspberry Pi 5 + 微雪
RGB-Matrix-P2-64x64 点阵屏上显示装甲板编号与目标图案，用于视觉算法的采集、
调试与验证。

内置素材包含 1–5 号装甲板、基地、前哨、哨兵，并提供裁剪到 64×64 的稳定版。

## 功能

- **直接显示图片**：`show-image`，支持完整显示 / 裁剪铺满 / 拉伸铺满与亮度调节。
- **GUI 选区工具**：`crop-selector`，在图片上拖出正方形区域，二值化、着色、
  旋转、画笔编辑，并实时投屏到 64×64 点阵屏，可导出 PNG。
- **稳定输出**：启动器用 `flock` 保证同一时间只有一个程序占用点阵屏；
  DMA 守卫在 RP1 PIO 卡死时自动退出（退出码 75）并由启动器重试。

## 硬件

| 部件 | 说明 |
|---|---|
| 主控 | Raspberry Pi 5 |
| 屏幕 | 微雪 RGB-Matrix-P2-64x64（HUB75，P2 间距，1/32 扫） |
| 供电 | 屏幕 5V 独立供电（5V/5A 以上），不要从 Pi 的 5V 引脚取电 |
| 接线 | 16P 排线直连，`hardware_mapping=regular`，注意方向与共地 |

## 环境准备

目标系统：Raspberry Pi OS 64-bit（Bookworm / Trixie），Python 3.11+。

### 1. 获取本仓库

```bash
git clone git@github.com:jianquwuying/RM_Armor_Identification.git
cd RM_Armor_Identification
```

### 2. 准备 RGB Matrix 库与 Python 绑定

Python 绑定链接微雪示例仓库中已编译的 `librgbmatrix.so.1`。先把示例仓库
放在本仓库旁边（目录名随意，下面以 `RGB-Matrix-Px-xx` 为例）：

```bash
git clone https://github.com/waveshareteam/RGB-Matrix-Px-xx.git
cd RGB-Matrix-Px-xx/example/Raspberry-Pi && make -C lib
```

回到本仓库创建 venv 并构建绑定：

```bash
cd RM_Armor_Identification
python3 -m venv --system-site-packages venv
venv/bin/pip install cython
./build-rgbmatrix-binding.sh ../RGB-Matrix-Px-xx/example/Raspberry-Pi
```

`build-rgbmatrix-binding.sh` 也接受第二个参数指定 venv 路径：

```bash
./build-rgbmatrix-binding.sh <示例仓库>/example/Raspberry-Pi <venv路径>
```

构建脚本会把 `core.so` / `graphics.so` 安装进 venv，并自动验证导入。

### 3. 运行时依赖

- Pillow（`--system-site-packages` 时通常已有 Debian 版本）
- 图形界面需要 Tk 与桌面环境（`DISPLAY`）

日常运行**不需要 sudo**；`sudo` 仅用于获取实时线程优先级，减少刷新抖动。

## 使用

### 显示图片

```bash
./show-image -i zhuangjiaban/Stable/1_stable.png
./show-image -i photo.jpg --mode fill -B 60
```

| 参数 | 说明 |
|---|---|
| `-i, --image PATH` | 图片路径（也可作为位置参数） |
| `-m, --mode fit\|fill\|stretch` | `fit` 完整显示留黑边（默认）；`fill` 裁剪铺满；`stretch` 拉伸铺满 |
| `-B, --brightness 0-100` | 屏幕亮度，默认 100 |
| `--show-refresh` | 周期性打印实际刷新率（调试） |

`Ctrl-C` 或 `SIGTERM` 退出，程序会清屏并释放 `/dev/pio0`。

### GUI 选区工具

```bash
./crop-selector                                  # 启动后选择图片
./crop-selector -i zhuangjiaban/1.jpg            # 直接打开图片
./crop-selector -i 1.jpg --region 100,200,600 --color red
```

操作：

- 拖动选区内部移动；拖动四角缩放（始终保持正方形）
- 方向键微调，`Shift+方向键` 10 像素；`+` / `-` 缩放；`Shift +/-` 10 像素
- `T` 开关二值化，`C` 循环输出颜色（白 → 红 → 蓝）
- `Ctrl-R` 旋转输出 90°；`Enter` 立即发送；`R` 重置选区
- 右侧为 64×64 预览，可保存 PNG；默认实时投屏
- `Esc` 退出

| 参数 | 说明 |
|---|---|
| `-i, --image PATH` | 启动时打开的图片 |
| `--region X,Y,SIZE` | 初始正方形选区（原图像素坐标） |
| `-B, --brightness 0-100` | 初始亮度 |
| `-t, --threshold 0-255` | 初始二值化阈值，默认 128 |
| `--color white\|red\|blue` | 二值化后亮像素颜色 |
| `--rotation 0\|90\|180\|270` | 输出顺时针旋转角度 |
| `--pwm-bits 1-11` | PWM 位深（越低刷新率越高） |
| `--luminance-correct / --no-luminance-correct` | CIE1931 亮度校正 |
| `--no-binarize` | 启动时关闭二值化 |

需要图形桌面；在 Pi 桌面上运行，或使用 `ssh -X`。

### 桌面启动项

`crop-selector.desktop` 可作为桌面图标或应用菜单项。复制到对应目录后，把
`Exec`、`Path`、`Icon` 中的路径改成你本机的仓库路径：

```bash
cp crop-selector.desktop ~/.local/share/applications/
```

## 素材

| 路径 | 内容 |
|---|---|
| `zhuangjiaban/1.jpg` … `5.jpg` | 1–5 号装甲板原始图 |
| `zhuangjiaban/jidi.jpg` | 基地 |
| `zhuangjiaban/outpost.jpg` | 前哨 |
| `zhuangjiaban/shaobing.jpg` | 哨兵 |
| `zhuangjiaban/Stable/*.png` | 上述图案的 64×64 二值化稳定版，可直接投屏 |

```bash
./show-image -i zhuangjiaban/Stable/shaobing_stable.png
```

## 稳定性说明

- **同一时间只能运行一个实例**：`show-image` / `crop-selector` 启动器都会
  `flock` 锁住 `/dev/pio0`。不要绕过启动器并发运行两个 Python 进程，否则两个
  进程会用不同状态机驱动同一组 GPIO，画面错乱或全黑。
- **不要按 `Ctrl-Z`**（SIGTSTP）：挂起会停止 PIO 刷新并锁死 `/dev/pio0`。
  脚本已忽略该信号。
- **退出流程**：清屏后用 `os._exit` 退出，不要 `del matrix`——该库的 PIO
  后端在析构时偶发死锁。
- **DMA 卡死自动恢复**：退出后立刻重开偶尔会因内核没有停干净 `dma2chan0`
  而黑屏。`rp1_dma_guard.py` 在显示前检查 DMA 中断计数，卡死时以退出码
  **75** 退出，启动器最多自动重试 4 次。若连续失败，可执行：

  ```bash
  sudo modprobe -r rp1_pio && sudo modprobe rp1_pio
  ```

## 目录结构

| 路径 | 说明 |
|---|---|
| `show-image.py` / `show-image` | 图片查看器与启动器 |
| `crop-selector.py` / `crop-selector` | GUI 选区工具与启动器 |
| `crop-selector.desktop` / `.png` | 桌面启动项与图标 |
| `rp1_dma_guard.py` | RP1 PIO / DMA 卡死检测 |
| `build-rgbmatrix-binding.sh` | 构建 rgbmatrix Python 绑定 |
| `zhuangjiaban/` | RoboMaster 装甲板与目标图案素材 |
| `venv/`、`build/` | 本地生成，不纳入版本控制 |
