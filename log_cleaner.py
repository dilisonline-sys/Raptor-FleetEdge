"""Background log cleaner — keeps only the last 24 hours of log entries per agent."""
import glob
import json
import os
import time
from datetime import datetime, timezone

LOG_PATTERN   = "/tmp/rfe_*.log"
RETAIN_HOURS  = 24


def clean_logs() -> dict:
    """
    Scan all /tmp/rfe_*.log files and drop any JSON-Lines entries older than
    RETAIN_HOURS. Returns a summary dict for reporting.
    """
    cutoff   = time.time() - RETAIN_HOURS * 3600
    summary  = {}

    for path in sorted(glob.glob(LOG_PATTERN)):
        try:
            with open(path, "r", errors="replace") as f:
                lines = f.readlines()

            kept = []
            dropped = 0
            for line in lines:
                line = line.rstrip("\n")
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                    ts_str = entry.get("ts", "")
                    if ts_str:
                        dt = datetime.fromisoformat(ts_str)
                        if dt.tzinfo is None:
                            dt = dt.replace(tzinfo=timezone.utc)
                        if dt.timestamp() < cutoff:
                            dropped += 1
                            continue
                except (json.JSONDecodeError, ValueError, KeyError):
                    pass  # keep malformed lines (don't lose them)
                kept.append(line)

            if dropped > 0:
                with open(path, "w") as f:
                    f.write("\n".join(kept) + ("\n" if kept else ""))
                summary[os.path.basename(path)] = {
                    "kept": len(kept), "dropped": dropped,
                    "size_kb": round(os.path.getsize(path) / 1024, 1),
                }
            else:
                summary[os.path.basename(path)] = {
                    "kept": len(kept), "dropped": 0,
                    "size_kb": round(os.path.getsize(path) / 1024, 1),
                }

        except Exception as e:
            summary[os.path.basename(path)] = {"error": str(e)}

    return summary


if __name__ == "__main__":
    result = clean_logs()
    for name, info in result.items():
        if "error" in info:
            print(f"  {name}: ERROR — {info['error']}")
        else:
            print(f"  {name}: kept={info['kept']} dropped={info['dropped']} size={info['size_kb']}KB")
