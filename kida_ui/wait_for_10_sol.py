import json
import time
import sys
import os

audit_path = "/home/liora/Documents/prjs/claude coomerce agents/aitrader/outputs/session_audit.json"
goal = 10.0

print(f"Waiting for realized PnL to reach {goal} SOL...")

while True:
    try:
        if os.path.exists(audit_path):
            with open(audit_path, "r") as f:
                data = json.load(f)
                pnl = data.get("total_realized_pnl_sol", 0.0)
                if pnl >= goal:
                    print(f"Goal reached! Current PnL: {pnl} SOL")
                    sys.exit(0)
    except Exception as e:
        pass
    time.sleep(30)
