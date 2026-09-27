"""汇总应用日志中的飞书交互阶段耗时，不连接线上服务。"""
import argparse
import json
import re
import sys
from collections import defaultdict
from pathlib import Path


STAGE = re.compile(
    r"rag_stage request_id=(?P<request_id>\S+) "
    r"stage=(?P<stage>\S+) elapsed_ms=(?P<elapsed>\d+(?:\.\d+)?) "
    r"outcome=(?P<outcome>\S+)"
)
KEY_STAGES = (
    "feishu_first_feedback", "feishu_total", "feishu_intent",
    "model_queue", "model_execution", "feishu_send", "service_total",
)


def percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    if not ordered:
        return 0.0
    position = (len(ordered) - 1) * fraction
    left = int(position)
    right = min(left + 1, len(ordered) - 1)
    return ordered[left] + (ordered[right] - ordered[left]) * (position - left)


def summarize(lines) -> dict:
    stages = defaultdict(list)
    failures = defaultdict(int)
    request_ids = set()
    for line in lines:
        match = STAGE.search(line)
        if match is None:
            continue
        name = match["stage"]
        stages[name].append(float(match["elapsed"]))
        failures[name] += match["outcome"] != "ok"
        if name == "feishu_total":
            request_ids.add(match["request_id"])
    return {
        "requests": len(request_ids),
        "stages": {
            name: {
                "count": len(stages[name]), "errors": failures[name],
                "p50_ms": round(percentile(stages[name], 0.5), 2),
                "p95_ms": round(percentile(stages[name], 0.95), 2),
            }
            for name in KEY_STAGES if stages[name]
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("log", nargs="?", type=Path,
                        help="应用日志路径；省略时读取标准输入")
    args = parser.parse_args()
    if args.log is None:
        result = summarize(sys.stdin)
    else:
        with args.log.open(encoding="utf-8") as stream:
            result = summarize(stream)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
