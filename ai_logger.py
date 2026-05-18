"""AI usage logger — records every LLM call with token counts and cost estimates."""
import json
import time
import os
from pathlib import Path

_LOG_FILE = Path(__file__).parent / "ai_usage.jsonl"

# Cost per 1M tokens in USD (update when pricing changes)
_PRICING = {
    "claude-haiku-4-5-20251001":      {"input": 0.80,  "output": 4.00},
    "claude-haiku-3-20240307":        {"input": 0.25,  "output": 1.25},
    "claude-sonnet-4-6":              {"input": 3.00,  "output": 15.00},
    "claude-opus-4-7":                {"input": 15.00, "output": 75.00},
    "gpt-4o-mini":                    {"input": 0.15,  "output": 0.60},
    "gpt-4o":                         {"input": 5.00,  "output": 15.00},
    "unknown":                        {"input": 1.00,  "output": 5.00},
}


def log_ai_call(
    purpose: str,
    model: str,
    input_tokens: int,
    output_tokens: int,
    extra: dict = None,
) -> float:
    """Log one AI call. Returns approximate cost in USD."""
    pricing = _PRICING.get(model, _PRICING["unknown"])
    cost = (input_tokens / 1_000_000) * pricing["input"] + \
           (output_tokens / 1_000_000) * pricing["output"]

    record = {
        "ts":            time.time(),
        "purpose":       purpose,
        "model":         model,
        "input_tokens":  input_tokens,
        "output_tokens": output_tokens,
        "cost_usd":      round(cost, 8),
        **(extra or {}),
    }

    with open(_LOG_FILE, "a") as f:
        f.write(json.dumps(record) + "\n")

    return cost


def get_summary(last_n: int = 20) -> dict:
    """Return summary stats and last N records."""
    if not _LOG_FILE.exists():
        return {"total_requests": 0, "total_cost_usd": 0.0,
                "total_input_tokens": 0, "total_output_tokens": 0, "records": []}

    records = []
    with open(_LOG_FILE) as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError:
                    pass

    total_cost   = sum(r.get("cost_usd", 0) for r in records)
    total_in     = sum(r.get("input_tokens", 0) for r in records)
    total_out    = sum(r.get("output_tokens", 0) for r in records)

    return {
        "total_requests":      len(records),
        "total_cost_usd":      round(total_cost, 6),
        "total_input_tokens":  total_in,
        "total_output_tokens": total_out,
        "records":             records[-last_n:],
    }
