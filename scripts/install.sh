#!/usr/bin/env sh

set -eu

SCRIPT_PATH="$0"
while [ -L "$SCRIPT_PATH" ]; do
	SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$SCRIPT_PATH")" && pwd)
	SCRIPT_PATH=$(readlink "$SCRIPT_PATH")
	case "$SCRIPT_PATH" in
		/*) ;;
		*) SCRIPT_PATH="$SCRIPT_DIR/$SCRIPT_PATH" ;;
	esac
done
SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$SCRIPT_PATH")" && pwd)
PROJECT_ROOT=$(CDPATH= cd -- "$SCRIPT_DIR/.." && pwd)
INSTALL_DIR="${YKT_INSTALL_DIR:-$HOME/.local/bin}"

mkdir -p "$INSTALL_DIR"
ln -sfn "$PROJECT_ROOT/scripts/ykt_signin" "$INSTALL_DIR/ykt_signin"
chmod +x "$PROJECT_ROOT/scripts/ykt_signin"

echo "已安装 ykt_signin -> $INSTALL_DIR/ykt_signin"
case ":${PATH:-}:" in
	*":$INSTALL_DIR:"*) ;;
	*) echo "请将 $INSTALL_DIR 加入 PATH，然后重新打开终端。" ;;
esac
echo "运行：ykt_signin"
