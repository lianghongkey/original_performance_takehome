# 计划：VLIW/SIMD 机器建模 + 提高代码生成成绩

> 配套设计见 [`DESIGN.md`](DESIGN.md)。节奏：**先建模、再说性能**。目标档位待模型出来后再定。

## 里程碑总览

| 阶段 | 产出 | 验收 | 状态 |
| - | - | - | - |
| P0 规格 & roofline | 机器规格吃透、上界估算 | 本文/DESIGN 记录在案 | ✅ 已完成 |
| P1 建 C++ 模型 | logix 上的忠实执行器 + 占用 trace | 对 Python 逐拍/逐字节 golden | ✅ 已完成 |
| P2 提高成绩 | 优化后的 `build_kernel` | `submission_tests.py` 拍数下降 | ✅ 147734→**1357**（108.9×，**过全部 9 档**）|

**P1 实测结果**：基线 kernel 在 C++ 模型上 `billed == 147734`、对 `frozen_problem.Machine` 逐字节
`mism=0`、交叉验证 7/7。基线 roofline：**alu-bound、下界 9899 拍**，实测 14.92×（没打包的空转），valu=0。
工具链：`bridge.py`（golden）/ `selftest.py`（交叉验证）/ `roofline.py`（瓶颈+下界）/ `--trace`（逐拍波形）。

**P2 实测结果（详见 [`RESULTS.md`](RESULTS.md)）**：147734 → 2107（向量化+打包）→ 1957（L0/L1 去重）
→ 1917（wrap-skip）→ 1745（group-major）→ 1616（L2 去重）→ 1481（**ALU 分流**）→ 1451（L3 线性 select）
→ 1412（MAC 留 valu）→ 1377（**pre-offset**）→ **1357**（L0 idx 特判 + 布局/去重比例调优）。
全程 golden `mism=0`、`tests/` 未改，**通过全部 9 档**（含 `<1363`，超过已知最好）。

---

## P0 — 规格与 roofline（已完成）

- [x] 读透 `problem.py` / `perf_takehome.py` / `submission_tests.py`，确认计费口径、slot 上限、
      §1.3 周期末提交、ISA 语义、内存布局、参考计算。
- [x] 复现基线 147734 拍。
- [x] 估算 roofline 双上界：valu 面 ~1365、load(gather) 面 ~2048。
- [x] 读透 logix 建模底座（`clock.h` / `module_writing_guide.md` / `engine_factory.h` / gmp uarch）。

---

## P1 — 建 C++ 性能模型 ✅ 已完成

### P1.0 落地位置定案（先做的小验证）
- [x] 按 DESIGN §3.5 **方案 B 一把过**：探针 `ClkModule` 用 `Makefile` include logix `src/` 头、
      链预编 `logix_base_static` + spdlog/hp-socket/sqlite3/fmt，自包含在 `./logix/` 编链跑通。
      **未触发方案 A 兜底**（无 glib 等传递依赖绊住）。

### P1.1 忠实执行器
- [x] `VliwCore : ClkModule`：`Cycle()` 取一条 bundle、逐引擎逐 slot 执行、周期末提交、`billed_`
      计费（DESIGN §3.1）。
- [x] `Exec` 覆盖全部 ISA slot 语义（alu / valu 含 `multiply_add`·`vbroadcast` / load 含 `vload`·
      gather / store 含 `vstore` / flow 含 `select`·`vselect`·跳转 / debug `compare`）。结果 `% 2**32`。
- [x] slot 上限 `LOGCHECK`；纯 debug bundle 不加 `billed_`。

### P1.2 Python↔C++ 桥 + golden
- [x] Python 侧薄导出：`KernelBuilder.instrs` + 初始 `mem` → JSON（不碰 `tests/`）。
- [x] C++ 读 JSON → 跑 → 输出最终 `mem` 段 + `billed` + 占用 trace。
- [x] 对拍脚本：C++ 的 `inp_values` / `billed` == Python `frozen_problem.Machine` 的结果。
      **验收门槛：`mism=0` 且 `billed` 与 `machine.cycle` 完全一致（先用基线 kernel 验，再用几个
      手改小 kernel 交叉验）。**

### P1.3 占用 trace / roofline
- [x] 逐引擎 `Trace("<engine>_busy", occ)` + `Trace("billed", …)`。
- [x] 用 `docs/tools/trace_stats.py` 出各引擎占用积分与逐段瓶颈，验证与 roofline 手算吻合。

### P1 验收
- 基线 kernel 在 C++ 模型上：最终内存逐字节等于 Python，`billed == 147734`。
- 能一键出「各引擎总 slot 数 / 上限 = 各自的拍数下界」表，指出当前瓶颈引擎。
- **到此「建模」完成，回到用户确认性能目标档位，再进 P2。**

---

## P2 — 提高代码生成成绩（建模后定档）

按收益递增，逐条在模型上量占用、回灌 Python 守 golden（DESIGN §4）。分档推进：

### P2.a 稳态向量化（预计入 ~1500 档）
- [x] 批 256 → 32 组 × VLEN8，走 valu / vload / vstore。
- [x] 批状态（idx/val）常驻 scratch，首轮 vload、末轮 vstore。
- [x] hash 阶段 0/2/4 折 `multiply_add`；idx 更新化简成 `2*idx+1+(val&1)` + wrap。
- [x] VLIW 打包：按 §1.3 依赖约束把独立算子填满每拍槽，跨元素并行填流水。

### P2.b 逼近 valu 上界（~1365）
- [x] 软流水调度器：把整个 `rounds×batch` 算子图列表调度成 bundle，逼近 roofline。
- [x] 用模型确认瓶颈是否已迁移到 load(gather) 面。

### P2.c 冲破 gather 瓶颈（冲高档才做）
- [x] 早期轮次 idx 去重/广播（round 0 全 idx=0 → 1 次 load 广播；逐轮唯一节点数递增）。
- [x] 用模型量 load 面占用下降，评估能否压到 valu 上界之下。

### P2 护栏
- [x] 每档改完跑 `python tests/submission_tests.py` 看过了哪几档、拍数多少。
- [x] `git diff origin/main tests/` 必须为空。
- [x] 不启用多核（本版刻意关闭）。

---

## 关键不变量（全程守）

1. `tests/` 一字不改；成绩以 `submission_tests.py` 为准。
2. C++ 模型语义分歧一律以 `problem.py` 为准，逐字节/逐拍对拍守 golden。
3. 计费只认非 debug bundle 数（`billed`），不是物理拍。
4. 模型退化为单 `ClkModule`，不引入无收益的 Factory/Fifo/延迟线。
