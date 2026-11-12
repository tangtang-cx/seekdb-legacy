# MiniVectorDB —— 2026 加赛交付包

轻量向量数据库，提交给 `2026-competition-round2` 加赛赛道的完整交付物。

## 1. 这是什么

| 项 | 值 |
| --- | --- |
| 赛题 | 赛题一：完成一个轻量向量数据库（加赛） |
| 总分 | 80 分（MySQL 用例准入一票否决 + 性能 40 + Agent Prompt 40） |
| 提测仓库 | `wzhaihqy/seekdb`（fork of `oceanbase/seekdb`）；团队副本 `tangtang-cx/apiay-off` |
| 提测分支 | `2026-competition-round2` |
| 修复提交 | `ef64c2a75` *fix(minivectordb): count VARCHAR length in characters and validate UPDATE up front*；性能提交见该分支最新提交 |

## 2. 目录说明

```
├── minivectordb/            实现主体（7 个文件，纯标准库，零 pip 依赖）
│   ├── core.py              值模型、错误码、float32 向量语义、行编码
│   ├── engine.py            目录、WAL、事务、SQL 执行器
│   ├── sqlparser.py         分词 + 递归下降解析器
│   ├── store.py             列式存储（mmap，内存与数据量解耦）
│   ├── fastpath.py          numpy 向量 TopK 快路径
│   └── server.py            JSONL stdin/stdout 协议服务
├── main.py                  程序入口
├── Dockerfile               判题器用的镜像（默认即 JSONL 模式）
├── verify_docker.sh         提交前一键容器自检
├── DESIGN.md                架构与设计说明
├── STDIO_PROTOCOL.md        官方通信协议（题包原文）
├── conversation.json        Agent Prompt 评价项使用的对话记录
├── agent/conversation.json  同上
├── mysqltest/               官方公开题包（20 个用例 / 95 条 SQL）
├── protocol_check/          官方自测驱动 run.py
├── docs/
│   ├── MiniVectorDB-赛题要点与施工清单.md   算分规则与 13 条功能准入项逐条状态
│   └── 修复说明-正式用例准入.md             本次修复的完整说明
└── tools/                   本地自建判题模拟器（非提测必需，仅用于复现）
```

## 3. 自己跑一遍

```bash
# 1) 跑官方公开用例（需要 Docker）
export VECTORDB_IMAGE='你的镜像名:标签'
python3 protocol_check/run.py --image "$VECTORDB_IMAGE" --handshake-only --output "protocol-results/handshake"
python3 protocol_check/run.py --image "$VECTORDB_IMAGE" --output "protocol-results/public"

# 2) 不依赖 Docker 的本地回归（Windows / Linux 均可）
python3 tools/mvdb_conformance.py    # 一次跑完五套检查（checks=263 failures=0）
python3 tools/mvdb_judge_sim.py     # 20 个公开用例 + 6 类隐藏场景
python3 tools/mvdb_sql_probe.py     # 66 条语句，逐条覆盖 13 条功能准入项
python3 tools/mvdb_differential.py  # 快路径 vs 通用执行器逐条对拍
python3 tools/mvdb_fuzz.py          # 随机对拍（queries=120 mismatches=0）
python3 tools/mvdb_crash_test.py    # 强杀持久性（crash persistence OK）
python3 tools/mvdb_profile.py       # 写阶段与查阶段的耗时分解
python3 tools/mvdb_bench.py         # 官方 benchmark 形状的基线
python3 tools/mvdb_bench_ab_e2e.py  # 向量化路径与通用执行器 A/B
```

`tools/` 下的两个脚本是本次为定位「1 个失败用例」而写的判题模拟器：
官方 `protocol_check/run.py` 依赖 Linux 的 `os.set_blocking` 语义，在 Windows 上
会把子进程的首行 stdout 写失败，所以改用等价实现，并额外覆盖公开题包**没有**用例的
场景：强杀后提交持久性、未提交事务不残留、恢复途中再杀、非法向量、超内存数据量、
ANN 召回与去重。

## 4. 关键设计

| 维度 | 做法 |
| --- | --- |
| 持久化 | 单一全局 WAL，提交点 fsync；checkpoint 记录 WAL 偏移与各表文件尺寸，恢复时截断回放 |
| 崩溃安全 | 数据文件只存已提交行；未提交数据只在 WAL 里；强杀后重放只保留已提交事务 |
| 内存 | 列式文件 mmap，只把主键索引常驻内存；向量列按块扫描，数据量可大于内存限额 |
| 事务 | 语句级 overlay 暂存，提交时才落到数据文件；失败语句不留部分效果 |
| 向量 | float32 存储语义，NaN / Infinity / 维度错 / float32 溢出全部拒绝 |
| TopK | numpy 向量化扫描（容器内 numpy 2.2.3）；无 numpy 时自动退回纯标准库路径，功能不变 |
| ANN | 当前返回精确结果，召回率 1.0，满足题包 ≥0.95 的门槛 |

## 5. 已知边界

- `CREATE VECTOR INDEX` 语句可用，但 ANN 路径目前走精确扫描（召回率 1.0，满足门槛，性能分因此偏低）。
- 嵌套事务、聚合、JOIN、子查询、ALTER、外键、NULL 值、主键更新按题面要求返回 `unsupported_sql`。
- 容器构建期若无法联网安装 numpy，会自动退回纯标准库路径：**功能与正确性不受影响**，只是 TopK 查询慢约 30 倍。
