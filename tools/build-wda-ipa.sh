#!/usr/bin/env bash
# 把 appium 提供的 WebDriverAgentRunner-Runner.zip 包成可被 Sideloadly 装的 .ipa。
#
# 用法：
#   1. 去 https://github.com/appium/WebDriverAgent/releases 下载
#      最新版 WebDriverAgentRunner-Runner.zip 到 ~/Downloads
#   2. ./tools/build-wda-ipa.sh
#   3. 桌面会出现 WebDriverAgentRunner-Runner.ipa，拖进 Sideloadly
#
# 为什么需要这一步：Apple 的 .ipa 格式要求把 .app 放进一个叫 Payload 的
# 文件夹再 zip。appium 的 release 直接给的是裸 .app，所以要本地包一下。
set -euo pipefail

ZIP_NAME="WebDriverAgentRunner-Runner.zip"
APP_NAME="WebDriverAgentRunner-Runner.app"
IPA_NAME="WebDriverAgentRunner-Runner.ipa"

# 优先在 ~/Downloads 找；找不到就在当前目录找
SEARCH_DIRS=("$HOME/Downloads" "$PWD")

SRC_ZIP=""
for dir in "${SEARCH_DIRS[@]}"; do
    if [[ -f "$dir/$ZIP_NAME" ]]; then
        SRC_ZIP="$dir/$ZIP_NAME"
        break
    fi
done

if [[ -z "$SRC_ZIP" ]]; then
    echo "❌ 没找到 $ZIP_NAME"
    echo "   去 https://github.com/appium/WebDriverAgent/releases 下载最新版到 ~/Downloads"
    exit 1
fi

echo "✓ 找到 $SRC_ZIP"

WORK_DIR="$(mktemp -d)"
trap 'rm -rf "$WORK_DIR"' EXIT

cd "$WORK_DIR"
unzip -q "$SRC_ZIP"

if [[ ! -d "$APP_NAME" ]]; then
    echo "❌ zip 里没有 $APP_NAME，appium 改 release 结构了？"
    exit 1
fi

mkdir Payload
mv "$APP_NAME" Payload/
zip -qr "$IPA_NAME" Payload

OUT_PATH="$HOME/Desktop/$IPA_NAME"
mv "$IPA_NAME" "$OUT_PATH"

echo "✓ 已生成 $OUT_PATH"
echo ""
echo "下一步：把 $IPA_NAME 拖进 Sideloadly，输 Apple ID 安装到 iPhone。"
