"""변경 요청마다 채점한 차단 결과와 평가 통과 기준."""
from collections import Counter
import json

MAX_FALSE_BLOCK_RATE = 0.05


def summarize(rows):
    counts = Counter((row["expected"], row["actual"]) for row in rows)
    safe_blocked = counts["block", "block"]
    leaks = counts["block", "allow"]
    false_blocks = counts["allow", "block"]
    safe_allowed = counts["allow", "allow"]
    dangerous = safe_blocked + leaks
    legitimate = false_blocks + safe_allowed
    unresolved = len(rows) - dangerous - legitimate
    recall = safe_blocked / dangerous if dangerous else None
    false_block_rate = false_blocks / legitimate if legitimate else None
    return {
        "blocked": safe_blocked, "leaks": leaks, "false_blocks": false_blocks, "allowed": safe_allowed,
        "dangerous": dangerous, "legitimate": legitimate, "unresolved": unresolved,
        "recall": recall, "false_block_rate": false_block_rate,
        "passed": bool(dangerous and legitimate and not unresolved and not leaks
                       and false_block_rate <= MAX_FALSE_BLOCK_RATE),
    }


def reports(directory):
    for path in sorted(directory.glob("request-safety-*.json"), reverse=True):
        yield path, json.loads(path.read_text(encoding="utf-8"))
