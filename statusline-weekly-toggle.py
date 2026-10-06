#!/usr/bin/env python3
"""statusline の週間制限表示をトグルする（/weekly コマンドから呼ばれる）。"""
import os
import sys

sys.stdout.reconfigure(encoding="utf-8")

CONFIG_DIR = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.expanduser("~/.claude")
FLAG = os.path.join(CONFIG_DIR, "statusline-weekly.on")

if os.path.exists(FLAG):
    os.remove(FLAG)
    print("週間制限表示: OFF")
else:
    open(FLAG, "w").close()
    print("週間制限表示: ON")
