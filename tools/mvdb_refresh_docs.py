"""Refresh the numbers in README.md and DESIGN.md after the optimisation round.

The performance figures in the documents come from the 2026-11-12 round;
this script replaces the round specific blocks with the measured values so
that the document and the code agree.

Usage:
    python tools/mvdb_refresh_docs.py
"""
from __future__ import annotations

import io
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

PERFORMANCE = """### 性能

性能分里查询占 2/3，所以优化以查询为主。基准复刻官方 benchmark 的形状：
10,000 行 × 128 维、1,000 次 TopK（K=10）、一半查询带普通列等值过滤、每 100 行提交一次。

**第一轮的结论是错的**：当时记录「查询 8.8 ms/次」，但复测基线是 **664.335 ms/次**。
原因是语法把带引号的向量字面量标成字符串节点，而向量化快路径只认向量节点，
于是那条快路径**从来没有被触发过**，每条查询都在走纯 Python 逐行扫描
（连 `WHERE id = 4242` 都要 716 ms 就是证据）。修正入口条件后，同一份数据：

| 场景 | 修正前 | 修正后 | 倍数 |
| --- | --- | --- | --- |
| 向量 TopK（1000 次） | 664.335 ms/query | 1.368 ms/query | 486× |
| 批量写入（100 批 × 100 行） | 3.667 s | 0.962 s | 3.8× |

随后按 cProfile 的自耗时排序逐项处理，各处改动的依据与效果：

| 改动 | 依据 | 效果 |
| --- | --- | --- |
| 向量化路径的入口接受带引号的向量字面量 | 基线 664 ms/次 + AST 打印 | 664 → 3.65 ms/次 |
| VARCHAR 等值过滤改为一次数组化比较（自耗时第一热点） | cProfile：compare_bytes 占 28% | 3.41 → 1.52 ms/次 |
| 向量字面量按文本记忆化（每查询解析 11 次 → 1 次） | cProfile：重复解析占 16% | 1.52 → 1.24 ms/次 |
| 小文件也建立 mmap 映射（每查询 121 次读系统调用 → 0） | cProfile：nt.read 121 次/查询 | 1.83 → 1.24 ms/次 |
| 写缓冲：每个列值一次 write → 每个文件每次提交一次 | cProfile：os.write 22 次/行 | 写 3.41 → 1.64 s |
| 字符串字面量改用 `str.find` 扫描（原来是逐字符转义） | cProfile：_decode_string 自耗时第一 | 写 2.60 → 1.43 s；解析 13.3 → 0.9 ms/100 行 |

A/B 复测用同一份数据、同一套 JSONL 协议，交替跑两条路径，基线是通用执行器
（`MINIVECTORDB_DISABLE_NUMPY=1`，即逐行解码 + 逐维算距离的参考实现）：

| 场景 | 通用执行器 | 向量化路径 | 倍数 |
| --- | --- | --- | --- |
| 向量 TopK | 664.335 ms/query | 1.368 ms/query | 486× |
| 批量写入（100 批 × 100 行） | 3.667 s | 0.962 s | 3.8× |

写入侧提速来自两处：向量字面量的整体解析，以及把写系统调用从「每个列值一次」
压到「每个文件每次提交一次」。查询侧提速来自把向量化路径真正打开，再去掉
四个被 cProfile 排在前列的重复工作。
"""

VERIFY_CONFORMANCE = (
    "| 自建一致性断言（按官方 README 点名的正式要求 + SQL 语义 + 建表/DML 边界 + 快路径语义） "
    "| `tools/mvdb_conformance.py` | checks=**263**，failures=**0** |")
VERIFY_DIFFERENTIAL = (
    "| 快路径 vs 通用执行器逐行一致 | `tools/mvdb_differential.py`（30 条固定语句） + "
    "`tools/mvdb_fuzz.py`（随机 120 条查询） | 30 条语句零差异；queries=120 **mismatches=0** |")
VERIFY_AB = (
    "| TopK A/B（走 JSONL 协议） | `tools/mvdb_bench_ab_e2e.py`，基线是通用执行器 | "
    "10000 行 / 1000 次查询：查询 664.335 → 1.368 ms/次；写入 3.667 → 0.962 s |")

TAIL = """另有一次实测直接指向「总分 0」的风险：向量字面量的负数解析失败。官方性能数据是随机向量，
必然含负号，这个 bug 会让功能准入整批失败。

7. **「向量化快路径已经生效」** — 被基线测量推翻：复测是 664 ms/次，而记录的结论是 8.8 ms/次。
   根因是语法把带引号的向量字面量标成字符串节点，快路径只认向量节点，
   于是那条写了很久的向量化路径从来没有被触发过，所有查询都在走纯 Python 逐行扫描。
   教训：**性能结论必须先证明目标路径真的被走到**，否则所有看起来合理的测量都会支持同一个错误结论。
8. **「缓存按文件长度失效就够了」** — 被语义用例推翻：UPDATE 不改变文件长度，
   常驻内存的 float64 列镜像因此保留了旧向量，改动跨越 TopK 边界时答案就错。
   现在写入向量行时同步 patch 镜像，无法 patch 时丢弃缓存。
9. **「快路径和通用路径长得一样就一样」** — 被差分测试推翻：快路径漏读了 `OFFSET`，
   `LIMIT 2 OFFSET 3` 返回了最前面的两行。差分测试（同一语句两条路径各跑一次）抓到它，
   人工读代码时它就藏在 `limit` 变量旁边。
"""


def section(lines, start_marker):
    start = None
    for index, line in enumerate(lines):
        if start is None and line.startswith(start_marker):
            start = index
        elif start is not None and line.startswith("---"):
            return start, index
    raise SystemExit("marker not found: %r" % start_marker)


def main():
    design_path = os.path.join(ROOT, "DESIGN.md")
    with io.open(design_path, encoding="utf-8") as handle:
        design = handle.read()
    lines = design.split("\n")
    start, end = section(lines, "### 性能")
    lines[start:end] = PERFORMANCE.split("\n")

    seen = 0
    for index, line in enumerate(lines):
        if line.startswith("| 自建一致性断言"):
            lines[index] = VERIFY_CONFORMANCE
        elif line.startswith("| 快路径 vs 通用执行器"):
            lines[index] = VERIFY_DIFFERENTIAL
        elif line.startswith("| TopK A/B") or line.startswith("| 端到端 A/B"):
            seen += 1
            if seen == 1:
                lines[index] = VERIFY_AB
            else:
                lines[index] = ""
    tail = None
    for index, line in enumerate(lines):
        if line.startswith("另有一次实测直接指向"):
            tail = index
            break
    if tail is None:
        raise SystemExit("the closing paragraph was not found")
    lines[tail:] = TAIL.split("\n")
    with io.open(design_path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write("\n".join(lines))

    readme_path = os.path.join(ROOT, "README.md")
    with io.open(readme_path, encoding="utf-8") as handle:
        readme = handle.read()
    readme = readme.replace(
        "| 提测仓库 | `liyunlong136/seekdb`（fork of `oceanbase/seekdb`） |",
        "| 提测仓库 | `wzhaihqy/seekdb`（fork of `oceanbase/seekdb`）；团队副本 `tangtang-cx/apiay-off` |")
    readme = readme.replace(
        "| 修复提交 | `ef64c2a75` *fix(minivectordb): count VARCHAR length in characters and validate UPDATE up front* |",
        "| 修复提交 | `ef64c2a75` *fix(minivectordb): count VARCHAR length in characters and validate UPDATE up front*；"
        "性能提交见该分支最新提交 |")
    readme = readme.replace(
        "python3 tools/mvdb_judge_sim.py       # 20 个公开用例 + 6 类隐藏场景\n"
        "python3 tools/mvdb_sql_probe.py       # 66 条语句，逐条覆盖 13 条功能准入项",
        "python3 tools/mvdb_conformance.py    # 一次跑完五套检查（checks=263 failures=0）\n"
        "python3 tools/mvdb_judge_sim.py     # 20 个公开用例 + 6 类隐藏场景\n"
        "python3 tools/mvdb_sql_probe.py     # 66 条语句，逐条覆盖 13 条功能准入项\n"
        "python3 tools/mvdb_differential.py  # 快路径 vs 通用执行器逐条对拍\n"
        "python3 tools/mvdb_fuzz.py          # 随机对拍（queries=120 mismatches=0）\n"
        "python3 tools/mvdb_crash_test.py    # 强杀持久性（crash persistence OK）\n"
        "python3 tools/mvdb_profile.py       # 写阶段与查阶段的耗时分解\n"
        "python3 tools/mvdb_bench.py         # 官方 benchmark 形状的基线\n"
        "python3 tools/mvdb_bench_ab_e2e.py  # 向量化路径与通用执行器 A/B")
    with io.open(readme_path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(readme)
    print("README.md and DESIGN.md refreshed")


if __name__ == "__main__":
    main()
