#!/usr/bin/env bash
# Flash the ESPHome bluetooth proxy factory image onto a Lolin D32 (ESP32, CH340).
# Usage: flash-lolin.sh [--erase] [--port /dev/ttyUSB0] [--bin path]
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
bin=""
port=""
erase=0

while [ $# -gt 0 ]; do
  case "$1" in
    --erase) erase=1 ;;
    --port) port="${2:?--port needs a value}"; shift ;;
    --bin) bin="${2:?--bin needs a value}"; shift ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
  shift
done

if [ -z "$bin" ]; then
  # Fresh `esphome compile` output first, then a manually kept copy.
  for candidate in \
    "$here/esphome/.esphome/build/btproxy-kitchen/.pioenvs/btproxy-kitchen/firmware.factory.bin" \
    "$here/esphome/build/btproxy-kitchen.factory.bin"; do
    [ -f "$candidate" ] && bin="$candidate" && break
  done
fi
[ -n "$bin" ] && [ -f "$bin" ] || { echo "no firmware: run 'esphome compile esphome/lolin-d32-btproxy.yaml' or pass --bin" >&2; exit 1; }
if [ -z "$port" ]; then
  for p in /dev/ttyUSB*; do [ -e "$p" ] && port="$p" && break; done
fi
[ -n "$port" ] || { echo "no /dev/ttyUSB* found: plug the board in (data cable)" >&2; exit 1; }
[ -r "$port" ] && [ -w "$port" ] || { echo "no access to $port: sudo usermod -aG dialout \$USER, then re-login" >&2; exit 1; }

echo "== chip_id on $port"
esptool --port "$port" chip-id

if [ "$erase" -eq 1 ]; then
  echo "== erase flash"
  esptool --port "$port" erase-flash
fi

echo "== write $bin @ 0x0"
esptool --chip esp32 --port "$port" --baud 460800 write-flash 0x0 "$bin"

echo "== logs (Ctrl+C to quit)"
exec esphome logs "$here/esphome/lolin-d32-btproxy.yaml" --device "$port"
