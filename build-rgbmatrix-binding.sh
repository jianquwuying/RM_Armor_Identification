#!/usr/bin/env bash
# 手动构建 rgbmatrix Python 绑定（core.so / graphics.so）。
#
# 不依赖 CMake / scikit-build-core：直接链接仓库中已编译好的
# librgbmatrix.so.1（由 `make -C lib` 生成），适合本机快速重建。
#
# 本脚本位于项目进度目录 progress/ 下；默认使用同目录的 venv，
# 构建中间文件放在同目录的 build/ 中。
#
# 用法：
#   ./build-rgbmatrix-binding.sh [仓库路径] [venv 路径]
# 默认：
#   仓库 <progress>/../example/Raspberry-Pi
#   venv <progress>/venv
set -euo pipefail

PROGRESS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="${1:-$PROGRESS_DIR/../example/Raspberry-Pi}"
VENV="${2:-$PROGRESS_DIR/venv}"
PY="$VENV/bin/python"
BUILD="$PROGRESS_DIR/build"
SRC="$REPO/bindings/python/rgbmatrix"
SHIMS="$SRC/shims"
LIB="$REPO/lib/librgbmatrix.so.1"

if [ ! -d "$REPO" ]; then
    echo "错误：仓库路径不存在：$REPO" >&2
    exit 1
fi
REPO="$(cd "$REPO" && pwd)"

for f in "$PY" "$LIB" "$SRC/core.pyx" "$SRC/graphics.pyx" "$SHIMS/pillow.c"; do
    if [ ! -e "$f" ]; then
        echo "错误：缺少 $f" >&2
        exit 1
    fi
done

if ! "$PY" -m cython --version >/dev/null 2>&1; then
    echo "错误：$VENV 中缺少 Cython。请运行：" >&2
    echo "  $VENV/bin/pip install cython" >&2
    exit 1
fi

mkdir -p "$BUILD"
SITE="$("$PY" -c 'import sysconfig; print(sysconfig.get_paths()["purelib"])')"
PKG="$SITE/rgbmatrix"
mkdir -p "$PKG"
PY_INC="$("$PY" -c 'import sysconfig; print(sysconfig.get_paths()["include"])')"

INCLUDES=(
    -I"$PY_INC"
    -I"$REPO/include"
    -I"$SHIMS"
    -I"$REPO/lib/rp1/rp1_pio_vendor/piolib/include"
    -I"$REPO/lib/rp1/rp1_pio_vendor/include"
)

echo "==> Cython 转译"
"$PY" -m cython --cplus "$SRC/core.pyx" --output-file "$BUILD/core.cpp"
"$PY" -m cython --cplus "$SRC/graphics.pyx" --output-file "$BUILD/graphics.cpp"

echo "==> 编译"
g++ -c -fPIC -O2 -g -std=c++17 "${INCLUDES[@]}" "$BUILD/core.cpp" -o "$BUILD/core.o"
gcc -c -fPIC -O2 -g -I"$PY_INC" -I"$SHIMS" "$SHIMS/pillow.c" -o "$BUILD/pillow.o"
g++ -c -fPIC -O2 -g -std=c++17 "${INCLUDES[@]}" "$BUILD/graphics.cpp" -o "$BUILD/graphics.o"

echo "==> 链接并安装到 $PKG"
g++ -shared -o "$PKG/core.so" "$BUILD/core.o" "$BUILD/pillow.o" \
    "$LIB" -Wl,-rpath,"$REPO/lib" -lpthread -lrt -lm
g++ -shared -o "$PKG/graphics.so" "$BUILD/graphics.o" \
    "$LIB" -Wl,-rpath,"$REPO/lib" -lpthread -lrt -lm
cp "$SRC/__init__.py" "$PKG/__init__.py"

echo "==> 验证导入"
"$PY" -c 'import rgbmatrix; print("rgbmatrix 绑定就绪：", rgbmatrix.__version__)'
"$PY" -c 'from rgbmatrix import graphics; print("graphics 绑定就绪")'
