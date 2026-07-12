# VLIW/SIMD 机器性能建模设计（takehome → logix）

> 用 logix 框架（对齐 gmp 示例的方法论）给 takehome 的这台 VLIW/SIMD 机器建一个**忠实的
> C++ 性能模型**，作为后续「提高代码生成成绩」的度量与驱动底座。本文是设计文档，配套计划见
> [`PLAN.md`](PLAN.md)。

---

## 0. 目标与阶段

节奏定为**先建模、再说性能**（用户口径）：

1. **建模（本轮重心）**：把 takehome 机器忠实重建成一个挂 `Clock` 的 `ClkModule`，逐引擎
   记 slot 占用、出 trace，对 Python 参照逐拍/逐字节 golden 一致。产出一个又快又可观测的
   执行器。
2. **优化（模型出来后再定档）**：用模型的占用统计做瓶颈定位，驱动 `KernelBuilder.build_kernel`
   的代码生成，把 `test_kernel_cycles` 的拍数往下打。目标档位到时再定。

**评分只认一个数**：`tests/submission_tests.py` 在冻结的 `frozen_problem.py` 上跑
`do_kernel_test(10, 16, 256)`，比对最终 `inp_values` 正确后取 `machine.cycle`。基线 147734 拍
（本仓已复现）。模型的唯一职责是**在优化过程中如实预测这个数、并指出该往哪使劲**。

---

## 1. 被建模对象：takehome 机器的精确规格

来源 `problem.py::Machine`。要忠实重建的语义如下。

### 1.1 计费口径（模型必须逐拍一致）

`run()` 里每步取 `program[pc]` 执行、`pc += 1`；**只有当这条 bundle 含非 debug slot 时**
`self.cycle += 1`。也就是：

- **一条 bundle = 至多 1 拍**；纯 debug 的 bundle **不计费**。
- 提交测试 `enable_debug=False`、`enable_pause=False` → 实测拍数 ≈ 含非 debug slot 的 bundle 条数。

### 1.2 引擎与每拍 slot 上限（这就是每拍吞吐）

```
alu=12   valu=6   load=2   store=2   flow=1   debug=64
VLEN=8   SCRATCH_SIZE=1536   N_CORES=1
```

`debug` 不占拍、提交时整个关掉，不参与建模的计费面（但功能校验时要能跑 `compare`）。

### 1.3 周期末统一提交（最关键的时序不变量）

`step()` 里先建空的 `scratch_write` / `mem_write`，一条 bundle 内**所有 slot 都读旧值**
（`core.scratch[...]` / `self.mem[...]`），把结果暂存进 `scratch_write` / `mem_write`；**全部 slot
执行完后**才把两张表刷回 `scratch` / `mem`。推论（决定调度合法性）：

- **同一 bundle 内的 RAW 危险**：若 slot B 读某地址、slot A 在同 bundle 写该地址，B 读到的是
  **旧值**。→ 真依赖的生产者与消费者**必须相隔 ≥1 条 bundle**。这条是后续 VLIW 打包/软流水
  的硬约束，256 路批内并行正是用来填这条流水的。
- 同 bundle 多个 slot 写同一地址：按引擎/ slot 迭代序，后写覆盖先写（`dict` 覆盖语义）。
- 同 bundle 内 `load` 与 `store` 撞同一内存地址：`load` 读旧、`store` 后落，互不影响本拍。

这套「上拍写、下拍读」正好是 logix `Latch` 的原生语义；模型里用「读齐后再提交」手工复刻即可，
不必真上 `Latch`（见 §3.2）。

### 1.4 ISA slot 语义（要在 C++ 里一一对齐）

- `alu(op, dest, a1, a2)`：`+ - * // cdiv ^ & | << >> % < ==`，结果 `% 2**32`。
- `valu`：`vbroadcast(dest,src)`、`multiply_add(dest,a,b,c)=(a*b+c)%2**32`、以及逐元素
  `(op,dest,a1,a2)` 对 `VLEN` 个 lane 各做一次 `alu`。**`multiply_add` 是后续 hash 折叠的关键算子**。
- `load`：`load(dest,addr)=mem[scratch[addr]]`（可做**数据相关 gather**）、`load_offset`、
  `vload(dest,addr)` 连续 8 个、`const(dest,val)`。
- `store`：`store(addr,src)`、`vstore(addr,src)` 连续 8 个。
- `flow`：`select / vselect / add_imm / halt / pause / cond_jump / cond_jump_rel / jump /
  jump_indirect / coreid / trace_write`。**只有 1 个 slot/拍**（易成瓶颈，优化时要少用）。
- `debug`：`compare(loc,key)` / `vcompare` —— 与 `value_trace` 对拍校验，免费、提交时关掉。

### 1.5 内存镜像与参考计算

`build_mem_image`：`header(7) + forest.values + inp.indices + inp.values + 冗余`；头 7 字放
`rounds/n_nodes/batch_size/height/forest_values_p/inp_indices_p/inp_values_p`。

`reference_kernel2` 的每 `(round, i)`：

```
idx = mem[inp_indices_p + i]
val = mem[inp_values_p + i]
node_val = mem[forest_values_p + idx]          # ← 数据相关 gather，无法 vload
val = myhash(val ^ node_val)                    # 6 阶段
idx = 2*idx + (1 if val%2==0 else 2)
idx = 0 if idx >= n_nodes else idx
mem[inp_values_p + i] = val ; mem[inp_indices_p + i] = idx
```

批内 256 路互相独立；跨 16 轮对同一 i 串行迭代。规模 `height=10 → n_nodes=2047`。

---

## 2. gmp 方法论 → 本模型的映射（含「退化」说明）

gmp 的做法：七个硬件单元各一个 `ClkModule`、同钟并发；数据面/控制面用 `Fifo<Pkt>` 握手、
`Factory` 记带宽与延迟、完成延迟线逐拍打拍；`Logic64` 记跨线程计数；`Trace`/insight 出逐拍占用，
再用 `trace_stats.py` 定瓶颈；功能数据留共享体、守逐字节 golden。

映射到 takehome 机器：

| gmp 概念 | 本模型落地 | 说明 |
| - | - | - |
| 硬件单元 = `ClkModule` | 一个 `VliwCore : ClkModule` | 单核；一条 bundle 一个 `Cycle()` |
| 单元每拍吞吐 | 引擎的 `SLOT_LIMITS` | alu12/valu6/load2/store2/flow1 = 每拍各自可执行的 slot 数 |
| 上拍写下拍读（Latch） | Cycle() 内「读齐→提交」 | §1.3，手工复刻，功能等价 |
| 功能数据留共享体 | `scratch[]` / `mem[]` 裸数组 | 仅 `Cycle()` 单协程触碰 → 免同步（指南 §4.3）|
| `Trace` + insight 占用 | 逐引擎 `Trace("alu_busy", n)…` | 每拍各引擎实占 slot 数 → roofline 数据 |
| golden 逐字节对拍 | 对 `problem.py` 比最终 mem + 拍数 | `mism=0`、`cycle` 完全一致 |

**必须明说的退化**：takehome 机器**没有多拍延迟**——任何 slot 的结果都在**下一拍**就可见，没有
可变延迟、没有带宽争用、没有 MSHR/回填/完成延迟线。所以本模型**不需要** `Factory` /
`Fifo<Pkt>` 跨模块协议 / 完成延迟线 / `Logic64` 计数器那一整套。它退化成一个单 `ClkModule`：
**逐引擎资源占用记账 + slot 上限合法性校验 + 计费拍计数 + 占用 trace**。刻意不过度建成
cycle-accurate 流水，否则是无收益的复杂度（这点在选型时已与用户确认）。

模型真正的价值有三个，都不靠「多拍时序」：

1. **快**：C++ 跑 ISA 远快于 Python，能在优化期高频评测大量候选调度。
2. **可观测**：逐引擎占用 trace → roofline（屋顶线上界分析）直接指出瓶颈引擎。
3. **可驱动**：作为调度器（Phase 2）的底座——把算子图按 slot 上限与 §1.3 依赖约束打包成
   bundle，预测拍数并产出优化后的程序。

---

## 3. 模型架构（C++ / logix）

### 3.1 顶层：`VliwCore : ClkModule`

`Cycle()` 体（对齐指南 §5「末级先做、起手 `DelayCycle(1)`、体内不再 yield」）：

```cpp
void Cycle() override {
  DelayCycle(1);
  if (state_ != RUNNING) { clk->Stop(); return; }
  if (pc_ >= program_.size()) { state_ = STOPPED; clk->Stop(); return; }

  const Bundle& b = program_[pc_++];
  pendScratch_.clear();  pendMem_.clear();     // 本拍写暂存
  bool hasNonDebug = false;

  for (auto& [engine, slots] : b) {
    if (engine == DEBUG) { RunDebug(slots); continue; }
    LOGCHECK(slots.size() <= kSlotLimit[engine], "slot 超限");
    for (auto& s : slots) Exec(engine, s);      // 读旧值 → 写 pend*
    hasNonDebug = true;
    occ_[engine] = slots.size();                // 本拍该引擎占用
  }

  for (auto& [a, v] : pendScratch_) scratch_[a] = v;   // 周期末提交
  for (auto& [a, v] : pendMem_]    ) mem_[a]     = v;

  if (hasNonDebug) ++billed_;                    // ← 计费拍（= submission 的 cycle）
  EmitOccTrace();                                // 逐引擎占用（提交后再 trace，指南 §7.4）
}
```

- `Exec` 按 §1.4 逐 slot 语义写；**所有读走 `scratch_` / `mem_`（旧值），所有写进 `pend*`**。
- `billed_` 就是要对齐 `machine.cycle` 的那个数；`clk` 推进的物理拍与它可以不同（纯 debug bundle
  推物理拍但不加 `billed_`；提交模式下两者一致）。

### 3.2 状态存储

`scratch_`（`SCRATCH_SIZE` 个 `uint32_t`）、`mem_`（`vector<uint32_t>`）都只被 `Cycle()` 这一个
协程触碰 → 按指南 §4.3 用**裸容器、免同步**。周期末提交手工做（§1.3），不上 `Latch`——因为不需要
跨模块可见、也没有第二个 writer。

### 3.3 占用 trace 与 roofline

每拍对每个非 debug 引擎 `Trace("<engine>_busy", occ)`，另 `Trace("billed", billed_)`。跑完用
`docs/tools/trace_stats.py` 出各引擎占用积分（= 该引擎总 slot 数）与逐段瓶颈。roofline 上界：

```
lower_bound_cycles = max_over_engine( 该引擎总 slot 数 / slot 上限 )
```

优化就是把这个 max 往下压，并让实际调度逼近它。

### 3.4 程序/内存 IO（Python ↔ C++ 桥）

为守 golden 与复用 Python 侧生成逻辑：

- Python 侧加一个薄导出：把 `KernelBuilder.instrs`（bundle 列表）+ 初始 `mem` 序列化成 JSON。
- C++ 模型读 JSON → 跑 → 输出最终 `mem` 段 + `billed` + 占用 trace。
- 对拍脚本比 C++ 的最终 `inp_values` / `billed` 与 Python 的 `machine`。**验收 = `mism=0` 且
  `billed` 完全一致**。

（Phase 2 若做 C++ 侧调度器，产出的程序同样回灌 Python 的 `frozen_problem.Machine` 复核，确保
不是「自己模型认自己对」。）

### 3.5 目录与构建落地（放置决策）

模型是建在 logix 框架上的 C++，需要 `base/clock.h`、`base/module.h`、`base/recorder.h` 等。两种落法：

- **方案 A（推荐，摩擦最小）**：C++ 模型作为 logix 仓的新 group —— `logix/src/vliwsim/*.h`
  （头文件模型）+ `logix/test/vliwsim/*.cpp`（gtest + golden），走既有 CMake/CTest。设计文档、
  计划、Python↔C++ 桥、优化后 kernel 都放在 takehome 侧 `./logix/`；在 `./logix/` 留一个指针
  README 指向 in-tree 模型。
- **方案 B（全部自包含在 `./logix/`）**：`./logix/` 下自带 `CMakeLists.txt`，`include` logix
  的 `src/` 头、链接预编的 `logix_base_static`。更贴合「建模到 ./logix」字面，但外部构建可能被
  logix 头的传递依赖（spdlog/libco/glib）绊住。

Phase 1 第一步先做一个小验证：优先按方案 B 试自包含构建；若被传递依赖绊住，退回方案 A。这一步
定下来后再铺开写模型（这属于实现细节，动手时即定，不阻塞本设计）。

---

## 4. Phase 2 预览：模型如何驱动「提高成绩」

roofline 已知的两条上界（`256×16` 次迭代）：

- **valu 面**：把 hash 阶段 0/2/4 折成一条 `multiply_add`（`val*k+c`）、把 `%2/==/select` 化简成
  `2*idx+1+(val&1)`、状态常驻 scratch（省掉每轮 load/store idx/val），每元素每轮 ~16 个 valu 算子
  → `256×16×16 / 8 / 6 ≈ 1365` 拍。
- **load 面**：`node_val` 是数据相关 gather，`256×16=4096` 次标量 load / 2 = **2048 拍**——这是不
  额外处理时的真瓶颈。要冲破 ~1363，必须**减少 gather 次数**（早期轮次 idx 高度重合 → 去重/广播等
  进阶技巧）。

优化手段清单（按预期收益，Phase 2 用模型逐条验证）：

1. SIMD 向量化：256 批分成 32 组 × VLEN 8，走 valu/vload/vstore。
2. VLIW 打包 + 软流水：按 §1.3 约束把独立算子塞满每拍的 12 alu / 6 valu / 2 load 槽，用跨元素
   并行填依赖流水。
3. hash 用 `multiply_add` 折叠（阶段 0/2/4 各省 2 个算子）。
4. idx 更新化简（去掉 `%`/`==`/`select`）。
5. 批状态常驻 scratch（`256+256` 字，SCRATCH_SIZE=1536 放得下），只首轮 vload、末轮 vstore。
6. （冲高档才做）gather 去重/广播，压 load 面到瓶颈之下。

每一步都在模型上量占用变化、确认瓶颈迁移，再回灌 Python 复核 golden——这正是 gmp「改一处、量占用、
守 golden」的闭环。

---

## 5. 风险与护栏

- **不改 `tests/`**：Readme 明确警告「模型改测试 = 作弊、无效」。所有校验以
  `tests/submission_tests.py` + `git diff origin/main tests/` 为空为准。
- **双实现漂移**：C++ 模型与 `problem.py` 必须逐字节/逐拍对拍守住，任何 ISA 语义分歧以 Python 为准。
- **过度建模**：明确退化为单 `ClkModule`（§2），不上 Factory/Fifo/延迟线。
- **计费口径**：`billed`（非 debug bundle 数）才是 `machine.cycle`，别把物理拍当成绩。
- **N_CORES=1**：多核在本版被刻意关掉（Readme 提示这是常见「假加速」作弊点），模型不做多核。
