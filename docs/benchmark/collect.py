#!/usr/bin/env python3
"""把 runs.jsonl 汇总成基准表（P50/P95、Critic 通过率、token 与成本）。"""
import json
import os
import statistics
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
RUNS = os.path.join(HERE, "results", "runs.jsonl")
OUT = os.path.join(HERE, "results", "summary.md")

# DeepSeek 公开价（USD / 1M tokens），off-peak：输入 cache-miss 0.15，输出 0.60
PRICE_IN = 0.15
PRICE_OUT = 0.60

# 各档素材的真实时长（秒），用于每 10 分钟归一
DURATIONS = {"short": 35.36, "mid": 339.8, "long": 939.8}
TIER_LABEL = {"short": "35 s", "mid": "5 min", "long": "15 min"}


def pct(values, p):
    if not values:
        return None
    values = sorted(values)
    if len(values) == 1:
        return values[0]
    # nearest-rank
    k = max(1, min(len(values), int(round(p / 100 * len(values) + 0.5))))
    return values[k - 1]


def load():
    if not os.path.exists(RUNS):
        sys.exit(f"missing {RUNS}")
    rows = []
    with open(RUNS, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            rows.append(json.loads(line))
    return rows


def main():
    rows = load()
    by_tier = {}
    for row in rows:
        by_tier.setdefault(row.get("tier"), []).append(row)

    lines = []
    lines.append("## 基准结果（deepseek-flash / Bailian ASR+Embedding / 本机 4C16G 无 GPU）\n")
    lines.append("| 时长 | 样本 | 成功 | P50 | P95 | Critic 通过率 | 平均 token(in/out) | 平均成本 | 每 10 min 成本 |")
    lines.append("| :--- | ---: | ---: | ---: | ---: | ---: | :--- | ---: | ---: |")

    detail = []
    for tier in ("short", "mid", "long"):
        group = by_tier.get(tier) or []
        if not group:
            continue
        ok = [r for r in group if r.get("state") == "COMPLETED"]
        walls = [r["wallMs"] for r in ok if r.get("wallMs")]
        tokens_in = [r["counters"].get("inputTokensEstimated", 0) for r in ok]
        tokens_out = [r["counters"].get("outputTokensEstimated", 0) for r in ok]
        passed = sum(1 for r in ok if r["counters"].get("criticPassed", 0) >= 1)
        avg_in = statistics.mean(tokens_in) if tokens_in else 0
        avg_out = statistics.mean(tokens_out) if tokens_out else 0
        cost = avg_in / 1e6 * PRICE_IN + avg_out / 1e6 * PRICE_OUT
        dur_min = DURATIONS[tier] / 60
        per10 = cost / dur_min * 10 if dur_min else 0

        p50 = pct(walls, 50)
        p95 = pct(walls, 95) if len(walls) >= 20 else None
        p95_text = f"{p95/1000:.0f} s" if p95 else "样本不足"
        lines.append(
            f"| {TIER_LABEL[tier]} | {len(group)} | {len(ok)} | "
            f"{(p50 or 0)/1000:.0f} s | {p95_text} | "
            f"{passed}/{len(ok) if ok else 0} | "
            f"{avg_in:.0f} / {avg_out:.0f} | ${cost:.4f} | ${per10:.4f} |")

        stages = [r.get("stageDurationMs") or {} for r in ok]
        def med(key):
            vals = [s.get(key) for s in stages if s.get(key)]
            return statistics.median(vals) if vals else 0
        model_calls = [r["counters"].get("modelCalls", 0) for r in ok]
        asr_calls = [r["counters"].get("asrCalls", 0) for r in ok]
        ocr_calls = [r["counters"].get("ocrCalls", 0) for r in ok]
        rounds = [r["counters"].get("criticRounds", 0) for r in ok]
        detail.append({
            "tier": tier, "n": len(group), "ok": len(ok),
            "p50": p50, "p95": p95, "walls_max": max(walls) if walls else 0,
            "stages": {k: med(k) for k in ("VIDEO_CONTEXT", "PLANNER", "EXECUTOR", "CRITIC", "REPLANNER", "AGENT_LOOP")},
            "modelCalls": statistics.mean(model_calls) if model_calls else 0,
            "asrCalls": statistics.mean(asr_calls) if asr_calls else 0,
            "ocrCalls": statistics.mean(ocr_calls) if ocr_calls else 0,
            "criticRounds": statistics.mean(rounds) if rounds else 0,
            "failed": [r for r in group if r.get("state") != "COMPLETED"],
        })

    lines.append("\n> 成本 = token × 公开价（输入 $0.15/1M，输出 $0.60/1M，off-peak cache-miss）；"
                 "token 数由服务端按字符估算，非账单真值。P95 仅在样本 ≥ 20 时给出。\n")

    lines.append("## 分阶段中位耗时\n")
    lines.append("| 时长 | 视频上下文 | Planner | Executor | Critic | Replanner | AgentLoop 总计 | 模型调用 | ASR 调用 | OCR 调用 |")
    lines.append("| :--- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |")
    for d in detail:
        s = d["stages"]
        lines.append(
            f"| {TIER_LABEL[d['tier']]} | {s['VIDEO_CONTEXT']/1000:.1f} s | {s['PLANNER']/1000:.1f} s | "
            f"{s['EXECUTOR']/1000:.1f} s | {s['CRITIC']/1000:.1f} s | {s['REPLANNER']/1000:.1f} s | "
            f"{s['AGENT_LOOP']/1000:.1f} s | {d['modelCalls']:.1f} | {d['asrCalls']:.1f} | {d['ocrCalls']:.1f} |")

    failures = [f for d in detail for f in d["failed"]]
    lines.append("\n## 失败样本\n")
    if failures:
        for f in failures:
            lines.append(f"- {f.get('tier')} run {f.get('run')}: {f.get('state')} / {f.get('message')}")
    else:
        lines.append("- 无")

    text = "\n".join(lines)
    with open(OUT, "w", encoding="utf-8") as fh:
        fh.write(text + "\n")
    print(text)
    print(f"\nwritten -> {OUT}")


if __name__ == "__main__":
    main()
