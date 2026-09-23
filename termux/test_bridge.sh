#!/data/data/com.termux/files/usr/bin/bash
curl -s http://127.0.0.1:8787/api/status | python -m json.tool 2>&1 | tee /dev/tty | termux-clipboard-set
