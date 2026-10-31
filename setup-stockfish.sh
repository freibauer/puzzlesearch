#!/bin/sh
# Download the official Stockfish Linux binary into ./engine/stockfish
# Usage: ./setup-stockfish.sh [asset]   (default: stockfish-linux-x86-64-universal.tar.gz)
set -e
cd "$(dirname "$0")"
case "$(uname -m)" in
  aarch64|arm64) DEFAULT=stockfish-linux-arm64-universal.tar.gz ;;
  *) DEFAULT=stockfish-linux-x86-64-universal.tar.gz ;;
esac
ASSET="${1:-$DEFAULT}"
URL="https://github.com/official-stockfish/Stockfish/releases/latest/download/$ASSET"
echo "Downloading $URL"
mkdir -p engine
TMP=$(mktemp -d)
curl -fsSL "$URL" -o "$TMP/sf.tar.gz"
tar -xf "$TMP/sf.tar.gz" -C "$TMP"
BIN=$(find "$TMP" -type f -name 'stockfish*' ! -name '*.tar*' -perm -u+x | head -1)
[ -z "$BIN" ] && BIN=$(find "$TMP" -type f -name 'stockfish-*' ! -name '*.tar*' | head -1)
cp "$BIN" engine/stockfish
chmod +x engine/stockfish
rm -rf "$TMP"
echo "Installed: $(echo quit | engine/stockfish | head -1)"
