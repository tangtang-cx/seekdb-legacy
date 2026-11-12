# MiniVectorDB 赛题要点与施工清单

> 题面来源：语雀《赛题一：完成一个轻量向量数据库》（doc_id 291289948）+《算分规则》（291296200）
> + 官方题包 `seekdb-round2/`（`STDIO_PROTOCOL.md`、`mysqltest/README.md`、`protocol_check/`）
> 抓取脚本：`tools/yuque-fetch.py`，抓下来的正文在 `yuque/` 目录。
> 最后核对时间：2026-11-11。

---

## 一、这条赛道到底怎么算分

总分 **80 分**，分两项独立排名；**功能准入不计分，但不过则总分 0**。

| 项目 | 分值 | 说明 |
|---|---|---|
| MySQL case 功能准入 | **不单独计分（一票否决）** | 正式用例共 **29 个**，全部通过才有计分资格；任一用例错误/崩溃/超时/资源超限 → 本次提交总分 0 |
| 小型性能 benchmark | 最高 **40 分** | `S_perf = max(40 − 0.5×(r−1), 0)` |
| Agent prompt 评价 | 最高 **40 分** | `S_prompt = max(40 − 0.5×(r−1), 0)`，原始分不封顶 |

性能细分（权重 60% 耗时 / 20% I/O / 20% 内存）：

| 性能项 | 耗时权重 | I/O 权重 | 内存权重 | 项内总权重 |
|---|---|---|---|---|
| 批量写入 | 20% | 6⅔% | 6⅔% | **33⅓%** |
| 向量 TopK 查询 | 40% | 13⅓% | 13⅓% | **66⅔%** |

综合指标 `E = (E_写入 + 2×E_查询) / 3`，`f(R) = R/(1+R)`，`R` 为相对基线的效率比。

**关键结论：查询项占性能分的 2/3，优化重点必须放在 TopK 查询，不在写入。**

性能评测的固定口径：

- 10,000 条记录、128 维向量、1,000 条 TopK 查询，K = 10，**一半查询带普通列等值过滤**。
- 写入按每批 100 条提交；写入耗时含持久化完成时间。
- 查询用 exact 还是 ann **在提交配置里固定**；两种模式的 Recall@10（有过滤/无过滤分别平均）都必须 ≥ **0.95**。
- 空集合返回空结果，不参与召回率平均；真值在**满足过滤条件的集合**内计算。
- 统计口径：cgroup v2 的 `io.stat`（rbytes+wbytes 增量）算 B、`memory.peak` 算 M（含页缓存）。
- 三轮取中位数。

---

## 二、硬性功能要求（题面 + 题包 README 合并后的清单）

来自 `mysqltest/README.md` 的「正式验收要求仍然公开」一段——**隐藏的是测试数据，不是功能要求**：

| # | 要求 | 状态 |
|---|---|---|
| 1 | CREATE TABLE；BIGINT、VARCHAR(n)、VECTOR(n)；每表一个 BIGINT 主键、至多一个向量列 | ✅ |
| 2 | INSERT / SELECT / UPDATE / DELETE；多行 INSERT；按主键或条件更新删除 | ✅ |
| 3 | 投影、SELECT \*、`= < <= > >=`、AND、ORDER BY、LIMIT | ✅ |
| 4 | L2_DISTANCE、精确 TopK、带普通列过滤的 TopK | ✅ |
| 5 | 拒绝 NaN、Infinity、维度错误、float32 溢出 | ✅（维度检查已前移到语句校验阶段） |
| 6 | 失败语句不能部分生效；显式事务之前成功的修改仍可提交或回滚 | ✅ |
| 7 | 已确认提交在进程强杀后保留，未提交修改不部分生效 | ✅ |
| 8 | **恢复途中再次退出仍能恢复**（重复恢复） | ✅ |
| 9 | **能处理超过内存限额的数据，不能只依赖全量常驻内存** | ✅（列式落盘，实测 3 万行峰值 RSS 20 MiB） |
| 10 | ANN 必须满足过滤、有效 ID、无重复及题包规定的召回率；索引更新不能破坏事务语义 | ✅（ann 模式当前返回精确结果，召回率 1.0） |
| 11 | `CREATE VECTOR INDEX name ON table(col)`；建索引后增删改必须正确反映到检索结果 | ⚠️ 语句已支持，但索引尚未真正生效（ann 走精确扫描） |
| 12 | 不支持的语句（NULL/JOIN/聚合/子查询/ALTER/外键/嵌套事务/主键更新）须返回约定错误 | ✅ |
| 13 | VARCHAR 长度按 Unicode 字符数；字符串按 Unicode 码点序比较 | ✅ |
| 14 | 并列距离按主键升序；普通 ORDER BY 并列也按主键升序 | ✅ |
| 15 | 恢复时索引重建完成前不得报告 ready | ✅（ready 在 `Engine.open()` 之后才发） |

### 提交产物要求（这一条最容易漏）

- **源码必须放在 `oceanbase/seekdb` 仓库的 `2026-competition-round2` 分支上**（个人开发者应 fork 后在自己的 fork 维护同名分支）。
- 提测时给：仓库 HTTPS URL + 分支名 `2026-competition-round2` + **完整 40 位 commit SHA**。
- 同一个 commit 里必须同时包含：**源码**、**Dockerfile**、**Agent prompt 材料 `conversation.json`**。
- `conversation.json` 路径：**仓库中的 `agent/conversation.json`**（评测入口默认读这里）。
- 格式硬约束：UTF-8、合法 JSON、**顶层为对象**、**≤ 128 KiB**、无重复键、无 NaN/Infinity；超限直接拒绝评审，不静默截断。
- 还需在仓库里保留：运行说明（语言版本/构建/启动/数据目录/SQL 范围/错误处理 + 从空目录开始的完整操作示例）、简要设计与验证记录（记录布局、向量索引方案、事务与提交持久化、恢复流程、已实现功能与已知限制）。

---

## 三、本轮已完成（2026-11-11）

### 存储层重写：全内存 → 列式落盘

原实现把行数据放在 `dict[int, list]` 里，**违反「不能依靠全量载入内存」**。现已改为：

```
/data/tables/<sha1(表名)>/ 
    pk.i64        int64  每 slot 的主键
    live.u8       uint8  每 slot 是否可见
    c<i>.dat      列数据 arena（BIGINT 定长 int64 / VECTOR 定长 float32 / VARCHAR 变长 blob）
    c<i>.null.u8  uint8  每 slot 是否为 NULL
    c<i>.off.u64  uint64 VARCHAR 在 blob 中的偏移
    c<i>.len.u32  uint32 VARCHAR 长度
```

- **只有主键索引常驻内存**：自写开放寻址哈希表（`array('q')` 双数组），约 **32 字节/行**，比 `dict` 省 3 倍。
- 读走 mmap（按 1 MiB 块惰性刷新），写走普通文件 IO——**写路径每追加一次都 `mmap.resize` 会让写入慢 5 倍**，这是实测出来的。
- 实测：**30,000 行 × 128 维（31.9 MiB 落盘）子进程峰值 RSS 仅 20 MiB**。

### 事务模型重写：数据文件只存已提交数据

这是崩溃恢复能过验收的关键。之前的模型是「语句执行即落盘 + WAL 兜底」，结果**未提交事务的行已经物理写进数据文件**，WAL 跳过不了它们——强杀后脏数据会活下来。

现在的模型：

1. 语句先把改动 **stage 进内存 overlay**，同时写 WAL 帧；
2. COMMIT 时先写 COMMIT 帧并 **fsync**（这一步之后事务才算持久），再把 overlay **物化**进数据文件；
3. ROLLBACK 只是丢弃 overlay，数据文件从未被碰过；
4. 自动提交语句同样走「先 WAL 后物化」。

于是数据文件里**永远只有已提交数据**，强杀恢复只需重放 WAL 即可，脏数据无处可藏。

### WAL checkpoint

- `checkpoint.json` 记录 WAL 偏移 + 每张表文件的形状；只在**干净事务边界**上做。
- 恢复时先把表文件截回 checkpoint 形状，再从记录的偏移重放 WAL。
- WAL 超过 64 MiB 自动 checkpoint 并截断 → **恢复耗时与数据量解耦**，而不是与历史写入量成正比。

### 语句级原子性

- 向量维度检查**前移到语句校验阶段**：带坏向量的 INSERT 在写 WAL 之前就失败，天然零副作用。
- 失败语句会撤销自己 stage 的 overlay 条目，之前语句的成功修改保留（符合「显式事务中之前成功的语句仍可继续提交或回滚」）。

### 自测工具

| 工具 | 作用 |
|---|---|
| `tools/run-local-check.ps1` | 免 Docker 跑官方题包 20 个公开用例（判定逻辑是官方原码，只替换了 POSIX-only 的 transport） |
| `tools/stress_check.py` | 规模 + 崩溃恢复自测：批量写入、峰值 RSS 采样、TopK、强杀恢复、重复恢复 |
| `tools/yuque-fetch.py` | 抓语雀全书正文（57/71 篇，含 MiniVectorDB 完整题面与算分规则） |
| `tools/docker-build-and-verify.ps1` / `.sh` | Docker 就位后一键 build + 官方判题器复验 + 复刻判题机启动参数 |

**当前验收：官方公开用例 20/20 PASS；规模与崩溃恢复自测全部通过。**

---

## 四、接下来要做的（按收益排序）

### P0 —— numpy 向量检索快路径（性能分 2/3 在这里）

现在 TopK 是**逐行解码 + 逐行 `math.dist`**：10,000 行 × 128 维 × 1,000 次查询，纯 Python 下每次查询要解码 1 万个向量，几百秒起步，直接超时。

做法（题面明确允许通用数学库）：

1. `minivectordb/` 里加 numpy 可选依赖，`import` 失败自动退回纯标准库路径（**镜像里 `pip install numpy` 用 `|| echo` 兜底，构建期无网也能过**）。
2. 存储层加**列缓存**：把向量列整体读成 numpy `(N, dim)` 的 float32 矩阵，缓存到写操作为止。
3. 检索走块级计算：`dist = sqrt(((mat - q)**2).sum(axis=1))`，一次算完整列。
4. 过滤条件也走 numpy 布尔掩码；**过滤必须先于 TopK**（题面明确：不能先在全表取 K 条再过滤）。
5. 距离用 float64 累加以匹配现有语义；float32 存储语义不变（容差 1e-5）。

预期：查询从「几百秒」降到「1~2 秒」，这是能不能进性能榜的分水岭。

### P1 —— 真正的 ANN 索引（可选，性能收益 vs 召回率风险）

- `CREATE VECTOR INDEX` 建 IVF 聚类索引，ann 模式下取 top-N 候选后**精确重排**。
- 召回率是硬门槛（Recall@10 ≥ 0.95），所以候选数要放大到 `k × 20` 以上再重排。
- **风险提示**：提测配置里 exact/ann 二选一。ann 快但召回率是硬约束；exact 慢但召回率必然 1.0。建议**先用 exact 打通性能分**，确认不超时后再考虑切 ann。

### P1 —— 提测材料（不做就交不了）

1. 把 `main.py` / `minivectordb/` / `Dockerfile` 放进 seekdb 仓库 `2026-competition-round2` 分支。
2. 写 `agent/conversation.json`：**≤ 128 KiB 的单个 JSON 对象**，把这次真实开发过程（提问 → 假设 → 实验 → 结果 → 修正 → 复测）按 `user_prompts` / `conversations` / `experiments` / `retrospective` / `limitations` 组织，证据用 **RFC6901 JSON pointer + ≤240 字符原文片段**定位。
3. 写运行说明与设计验证记录（题包 4.2 / 4.3）。
4. 提交时给 HTTPS URL + 分支名 + 40 位 commit SHA。

### P2 —— Agent prompt 40 分怎么拿

规则是 `agent-learning-evidence-v3-unbounded`：五个维度（问题理解与自主拆解 / 技术理解与迁移 / 独立验证与质疑 / 实验与反馈迭代 / 取舍与反思），每个独立实例按 0.5 / 1 / 1.5 / 2 四档取最高适用档位，**原始分不封顶**，独立评审三次取中位数。

也就是说：**分数来自「有证据的独立思考实例」，不是来自代码量，也不是来自对话长度。** 这一轮已经天然攒下了不少实例，例如：

- 预判「全内存 RowStore 违反超内存要求」→ 实测列式落盘后 3 万行峰值 RSS 20 MiB（1.5 档：解释结果 + 复测）。
- 发现 `mmap.resize` 主导写入耗时 → profile 定位 → 改成按块惰性刷新 → 写入吞吐 2760 → 13859 行/s（2 档：迁移 + 分析局限）。
- Windows 下 `os.open` 缺 `O_BINARY` 导致文本模式把 `0x0a` 写成 `0x0d 0x0a`、定长列整体错位 → 用公开用例回归交叉验证（2 档）。
- 崩溃恢复里「未提交行已落盘」的推断 → 引入 overlay 模型 → 强杀 + 重复恢复双验证（2 档）。

把这类实例按 JSON pointer 精确引用写进 `conversation.json`，比任何总结性描述都值钱。**注意：必须真实，不能编造交互或实验结果。**

---

## 五、必须知道的环境约束

- 判题容器：`docker run -i --network=none --cpus=2 --memory=2g --memory-swap=2g --pids-limit=128 --read-only --cap-drop=ALL --security-opt=no-new-privileges`，`/data` 是唯一可写挂载点 → **所有持久化文件必须落在 `/data`**。
- 正式执行**禁止联网**，依赖必须在构建期装好并锁版本（numpy 已用 `|| echo` 兜底）。
- 启动必须**主动**输出并 flush `{"type":"ready"}`；`shutdown` 后零输出、退出码 0；日志只走 stderr。
- `id` 原样回传且保持 JSON 类型；`ok` 必须是 JSON 布尔；不输出 NaN/Infinity/重复键。
- 崩溃模型是**进程被强制终止**，不模拟断电或磁盘损坏；「已发出 COMMIT 但未收到成功响应」的事务允许完整生效或完整不生效，**不能部分生效**——本实现的顺序（WAL 先 fsync、再物化）天然满足。
- 每题每天最多正式提交 **2 次**；预检不占额度；截止 **2026-11-18 12:00**。
