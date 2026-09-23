#!/data/data/com.termux/files/usr/bin/bash
cd "$(dirname "$0")"
python coinswitch_bridge.py 2>&1 | tee /dev/tty | termux-clipboard-set
