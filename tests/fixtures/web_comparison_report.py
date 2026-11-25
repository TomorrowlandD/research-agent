"""Verify the historical and paired web-eval inputs, then render the comparison."""
import argparse
from collections import Counter
import json
import math
from pathlib import Path
from statistics import mean, median, quantiles

DOCS = Path(__file__).resolve().parents[2] / "docs" / "02-验收记录"
HISTORICAL = {"P0": "003-P0基线.json", "P1": "003-P1联网验收.json"}
ANSWERS = {"static": "NL-4827", "interactive": "SW-6319"}
LABELS = {"static": "静态查询", "interactive": "动态点击"}


def verify(row, calls):
    case = row["case"]
    evidence = any(c["ok"] and (c["tool"] == "web_fetch" or c["tool"].startswith("mcp__playwright__")) for c in calls)
    passed = row["answer"] == ANSWERS[case] and evidence
    if (row["expected"] != ANSWERS[case] or row["passed"] != passed or row["tool_calls"] != len(calls)
            or not math.isfinite(row["seconds"]) or row["seconds"] <= 0):
        raise ValueError(f"Inconsistent outcome in {case} at {row.get('started_utc', row.get('trace_id'))}")
    for key in ("main_tokens", "side_tokens"):
        if any(type(row[key][k]) is not int or row[key][k] < 0 for k in ("input", "output")):
            raise ValueError("Invalid token counts")


def tokens(row, field):
    return row["main_tokens"][field] + row["side_tokens"][field]


def metrics(rows):
    times = [r["seconds"] for r in rows]
    return {"p50": median(times), "p95": quantiles(times, n=100, method="inclusive")[94],
            "tools": mean(r["tool_calls"] for r in rows), "input": mean(tokens(r, "input") for r in rows),
            "output": mean(tokens(r, "output") for r in rows), "passed": sum(r["passed"] for r in rows)}


def render(path):
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    pairs = Counter((r["repeat"], r["case"], r["arm"]) for r in rows)
    n = max(r["repeat"] for r in rows)
    expected = {(i, case, arm) for i in range(1, n + 1) for case in ANSWERS for arm in HISTORICAL}
    if set(pairs) != expected or set(pairs.values()) != {1} or n < 2:
        raise ValueError("Incomplete or duplicated paired samples")
    for r in rows:
        verify(r, r["calls"])
        revision = {"P0": "8702527", "P1": "3a999e5"}[r["arm"]]
        if r["model"] != "deepseek-v4-pro" or not r["revision"].startswith(revision):
            raise ValueError("Unexpected model or revision")

    lines = ["# 003 P2.4 联网能力量化对照", "",
             f"新增 {n} 轮配对实测，共 {len(rows)} 次独立的端到端任务。"
             f"覆盖 UTC {rows[0]['started_utc']} 至最后一项开始时间 {rows[-1]['started_utc']}。",
             "", "## 同条件实测", "",
             "| 任务 | 路径 | 成功 / 样本 | 成功率 | P50 秒 | P95 秒 | 平均工具调用 | 平均输入 Token | 平均输出 Token |",
             "| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |"]
    summary = {}
    for case, label in LABELS.items():
        for arm in HISTORICAL:
            group = [r for r in rows if (r["arm"], r["case"]) == (arm, case)]
            m = summary[case, arm] = metrics(group)
            lines.append(f"| {label} | {arm} | {m['passed']}/{n} | {100*m['passed']/n:.0f}% | "
                         f"{m['p50']:.3f} | {m['p95']:.3f} | {m['tools']:.2f} | {m['input']:,.1f} | {m['output']:,.1f} |")
    lines += ["", "P1 相对 P0 的变化按各组汇总值计算。", "",
              "| 任务 | P50 时延变化 | P95 时延变化 | 平均输入 Token 变化 | 平均工具调用差值 |",
              "| --- | ---: | ---: | ---: | ---: |"]
    for case, label in LABELS.items():
        old, new = summary[case, "P0"], summary[case, "P1"]
        deltas = [f"{100*(new[k]/old[k]-1):+.1f}%" for k in ("p50", "p95", "input")]
        lines.append(f"| {label} | " + " | ".join(deltas) + f" | {new['tools']-old['tools']:+.2f} |")

    lines += ["", "## 实际工具路径", "",
              "| 任务 | 路径 | 工具调用顺序 | 次数 |", "| --- | --- | --- | ---: |"]
    for case, label in LABELS.items():
        for arm in HISTORICAL:
            sequences = Counter(tuple(c["tool"].removeprefix("mcp__playwright__browser_") for c in r["calls"])
                                for r in rows if (r["arm"], r["case"]) == (arm, case))
            for sequence, count in sequences.most_common():
                lines.append(f"| {label} | {arm} | {' → '.join(sequence) or '无调用'} | {count} |")

    failures = [r for r in rows if not r["passed"]]
    if failures:
        lines += ["", "## 未通过的实测样本", "",
                  "| 轮次 | 路径 | 任务 | 预期编号 | 最终答复 | 成功工具 / 总调用 |",
                  "| ---: | --- | --- | --- | --- | ---: |"]
        for row in failures:
            ok = sum(call["ok"] for call in row["calls"])
            lines.append(f"| {row['repeat']} | {row['arm']} | {LABELS[row['case']]} | "
                         f"{row['expected']} | {row['answer']} | {ok}/{row['tool_calls']} |")
        lines += ["", "上述样本计入成功率、时延分位数和各项均值。"]

    lines += ["", "## 历史单次观测", "",
              "逐条核对已有 JSON 的最终编号、成功标志、工具列表与 span、耗时及模型输入／输出 Token。每个格子只有一次观测，独立列示。",
              "", "| 任务 | 路径 | 最终编号 | 成功 | 耗时（秒） | 工具调用 | 输入 Token | 输出 Token |",
              "| --- | --- | --- | --- | ---: | ---: | ---: | ---: |"]
    for arm, filename in HISTORICAL.items():
        history = json.loads((DOCS / filename).read_text(encoding="utf-8"))
        if Counter(r["case"] for r in history) != Counter(ANSWERS.keys()):
            raise ValueError("Incomplete historical observations")
        for r in history:
            calls = [{"tool": s["tool_name"], "ok": s["ok"]} for s in r["spans"]]
            if r["tools"] != [c["tool"] for c in calls]:
                raise ValueError("Historical trace differs from tool sequence")
            verify(r, calls)
            lines.append(f"| {LABELS[r['case']]} | {arm} | {r['answer']} | {r['passed']} | {r['seconds']:.3f} | "
                         f"{r['tool_calls']} | {tokens(r,'input'):,} | {tokens(r,'output'):,} |")
    lines += ["", "## 比较条件与复核方式", "",
              "- P0 历史记录使用 `8702527` 加当时工作区改动。Git 未保存独立的 P0 验收快照。新增 P0 组运行 `8702527` 的 Agent 源码，在临时工作区使用与 P1 一致的 Linux 启动命令及锁定版本，恢复主 Agent 直连完整浏览器工具目录。此组属于 P0 路径重建。",
              "- P1 组运行 `3a999e5` 的运行时及其 MCP 角色、白名单配置。两个版本均从 Git 导出到临时目录；观察器在工具执行入口计数。逐条数据保留版本、角色、调用序列、浏览器工具目录大小和模型计量。",
              "- 两题沿用既有页面和提示。使用 WSL Ubuntu 24.04.1、Python 3.11.18、Node 26.13.1、MCP SDK 2.3.0、Playwright MCP 0.0.86、同一 DeepSeek 模型及账号。npm 缓存已预热，Chromium 与共享库相同。每次创建独立进程、临时 HOME、工作区及 Agent。每个任务限 8 轮、单次输出上限 1,500 Token，关闭记忆预取、折叠及在线技能演化。",
              "- 每轮两题使用同一回环页面服务，两个版本串行交替先后。耗时从 run_once 开始到返回，包含模型推理、工具执行及运行时内的首次 MCP 初始化，主会话关闭计时在外。输入、输出包含主会话累计的子代理用量，并加辅助调用；工具数包含委派。成功要求最终编号匹配且存在成功的网页工具调用。",
              f"- P50 取中位数，P95 在排序后的相邻样本间线性插值（位置 0.95 × (n−1)，从 0 计）。每组 n={n}；P95 对慢样本敏感。结果适用于这两道固定题及本次服务延迟，开放检索质量与高并发延迟另需实测。",
              "", f"[逐次实测 JSONL]({path.name}) · [历史 P0 JSON](003-P0基线.json) · [历史 P1 JSON](003-P1联网验收.json)", "",
              "从仓库根目录运行以下命令，使用原始数据重建报告或使用新路径采集独立样本。", "", "```bash",
              f"python -s -B tests/fixtures/web_comparison_report.py 'docs/02-验收记录/{path.name}' --output /tmp/web-comparison.md",
              f"PYTHONPATH=. python -s -B tests/fixtures/web_comparison.py collect /tmp/web-comparison-new.jsonl --rounds {n}", "```", ""]
    return "\n".join(lines)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("data", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.write_text(render(args.data), encoding="utf-8")
