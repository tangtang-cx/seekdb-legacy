"""Refresh the final numbers of the 2026-11-12 round in the material and the
documents, and add the last experiment of the round.

Usage:
    python tools/mvdb_final_update.py
"""
from __future__ import annotations

import io
import json
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MATERIAL = os.path.join(ROOT, "agent", "conversation.json")

EXPERIMENT = {
    "id": "e29",
    "title": "距离公式换成一次矩阵乘法（并处理抵消误差）",
    "question": "扫描阶段每查询要把整列搬三遍（减法缓冲写一遍、再读一遍求平方和），"
               "能不能只搬一遍？",
    "hypothesis": "如果把每行的平方范数缓存下来，距离可以写成 |a-q|^2 = |a|^2 - 2a.q + |q|^2，"
                  "每块就只剩一次矩阵乘法；代价是这个式子在查询向量与某一行完全相同时会抵消，"
                  "所以要量一下误差有多大。",
    "method": "先在 20000 行 × 128 维上分别用参考实现（逐维顺序求和）和公式计算距离，"
              "统计最大绝对偏差；再把查询向量取成某一行本身，单独量抵消情形；"
              "然后把公式接进扫描，并在候选筛选上留出容差。",
    "output_before_fix": "公式与参考实现的最大绝对偏差：普通情形 7.1e-15；"
                         "查询向量与某一行完全相同时 1.19e-7（平方项相消，"
                         "d^2 的 1e-14 级误差被开方放大到 1e-7）。"
                         "扫描阶段的搬运量：float64 镜像 10.24 MB + 差值缓冲写 10.24 MB + "
                         "einsum 读 10.24 MB。",
    "fix": "store 增加按列缓存的行平方范数（与 float64 镜像同寿命，写入时同步 patch）；"
           "扫描改成每块一次矩阵乘法；候选筛选的容差从相对 1e-9 放宽到「绝对 1e-6 或相对 1e-9」，"
           "覆盖抵消情形；上报的距离仍然用参考实现复算。",
    "output_after_fix": "扫描阶段 0.730 ms → 0.497 ms（micro 口径）；"
                        "查询阶段 1.368 ms/次 → 1.149 ms/次（端到端 1000 次查询）；"
                        "写入阶段 1.011 s。conformance 仍然 checks=263 failures=0。",
    "conclusion": "公式本身是精确的（偏差 7e-15，与差值形式同量级），"
                  "唯一的例外是查询向量与某一行完全相同时的抵消，误差 1.2e-7。"
                  "把这个误差量出来之后，容差就能按 1e-6 来定，"
                  "而 1e-6 远小于相邻距离的间隔，不会把无关的行拉进候选。"
                  "结论是：先用反例量出误差边界，再决定能不能换公式，"
                  "而不是先换公式再找理由。",
    "status": "executed",
    "executed": True,
}

RETROSPECTIVE_OLD = "查询从 664 ms/次降到 1.37 ms/次、写入从 3.67 s 降到 0.96 s。"
RETROSPECTIVE_NEW = "查询从 664 ms/次降到 1.15 ms/次、写入从 3.67 s 降到 1.01 s。"

DOC_OLD = """| 向量 TopK（1000 次） | 664.335 ms/query | 1.368 ms/query | 486× |
| 批量写入（100 批 × 100 行） | 3.667 s | 0.962 s | 3.8× |"""
DOC_NEW = """| 向量 TopK（1000 次） | 664.335 ms/query | 1.149 ms/query | 578× |
| 批量写入（100 批 × 100 行） | 3.667 s | 1.011 s | 3.6× |"""

DOC_OLD_AB = """| 向量 TopK | 664.335 ms/query | 1.368 ms/query | 486× |
| 批量写入（100 批 × 100 行） | 3.667 s | 0.962 s | 3.8× |"""
DOC_NEW_AB = """| 向量 TopK | 664.335 ms/query | 1.149 ms/query | 578× |
| 批量写入（100 批 × 100 行） | 3.667 s | 1.011 s | 3.6× |"""

VERIFY_OLD = ("10000 行 / 1000 次查询：查询 664.335 → 1.368 ms/次；写入 3.667 → 0.962 s |")
VERIFY_NEW = ("10000 行 / 1000 次查询：查询 664.335 → 1.149 ms/次；写入 3.667 → 1.011 s |")


def main():
    with io.open(MATERIAL, encoding="utf-8") as handle:
        data = json.load(handle)
    if not any(item["id"] == "e29" for item in data["experiments"]):
        data["experiments"].append(EXPERIMENT)
    data["retrospective"] = data["retrospective"].replace(RETROSPECTIVE_OLD,
                                                           RETROSPECTIVE_NEW)
    data["retrospective"] = data["retrospective"].replace(
        "这一轮的技术选择基本都是「先测量再改」",
        "这一轮的技术选择基本都是「先测量再改」")
    with io.open(MATERIAL, "w", encoding="utf-8") as handle:
        json.dump(data, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    print("material: %d bytes, %d experiments"
          % (os.path.getsize(MATERIAL), len(data["experiments"])))

    design_path = os.path.join(ROOT, "DESIGN.md")
    with io.open(design_path, encoding="utf-8", newline="") as handle:
        design = handle.read().replace("\r\n", "\n")
    for old, new in ((DOC_OLD, DOC_NEW), (DOC_OLD_AB, DOC_NEW_AB),
                     (VERIFY_OLD, VERIFY_NEW)):
        design = design.replace(old, new)
    if "距离公式换成一次矩阵乘法" not in design:
        design = design.replace(
            "| 改动 | 依据 | 效果 |",
            "| 改动 | 依据 | 效果 |\n"
            "| 距离改成 |a|²-2a.q+|q|²，每块一次矩阵乘法（搬运量减到 1/3） | "
            "微基准：扫描 0.730 → 0.497 ms；抵消误差实测 1.19e-7，容差取 1e-6 | "
            "1.37 → 1.15 ms/次 |",
            1)
    with io.open(design_path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(design)
    print("DESIGN.md refreshed")


if __name__ == "__main__":
    main()
