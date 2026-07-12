# 结果：模型驱动把代码生成成绩从 147734 打到 1131（130.6×，过全部 9 档）

配套：[`DESIGN.md`](DESIGN.md)（建模）、[`PLAN.md`](PLAN.md)（计划）。

## 一句话

用 logix 建的忠实 C++ 性能模型 + roofline/trace 逐步定位瓶颈，把 `submission_tests.py` 的拍数从基线
**147734 → 1131（130.6×）**，全程对 `frozen_problem.Machine` 逐字节 `mism=0`、`tests/` 一字未改，
**通过全部 9 个测试**（含 `<1363`，即超过 Readme 里 Opus 4.5 improved-harness 的 1363 与邮件门槛 1487）。

## 优化轨迹（每步都由 model 的 roofline/trace 指出下一个瓶颈）

| 拍数 | 加速 | 瓶颈 | 关键手法 |
| - | - | - | - |
| 147734 | 1.0× | alu（每 bundle 单 slot） | 基线 |
| 2107 | 70.1× | **load(gather) 2074** | 256→32组×VLEN8；hash 阶段0/2/4 折 multiply_add；idx 化简免 flow；状态常驻 scratch；依赖感知 VLIW 打包（1.02× 逼近 roofline）|
| 1957 | 75.5× | valu 1630 | 同步层级性质：第 r 轮所有 idx 在第 r%11 层。L0 广播、L1 select 免 gather |
| 1917 | 77.1× | load 1563 | wrap 只有 level==height 那轮触发 → 其余轮省；末轮 idx 无用 → 省 |
| 1745 | 84.7× | load 1563 | **group-major 发射**：不同组错位在不同层 → valu 组与 load 组同时忙，消 load 空转 |
| 1616 | 91.4× | valu 1466 | L2 select 去重 |
| 1481 | 99.8× | alu/load 1310 | **ALU 分流**：闲置的 alu（12 槽）跑部分组的标量 hash，valu+alu 双引擎 |
| 1451 | 101.8× | compute 1304 | L3 **线性 select** 去重（只用 2 临时、省 scratch）|
| 1412 | 104.6× | valu 1236 | **MAC 留 valu、只非-MAC 分流 alu**（避标量 MAC 的 2× 惩罚），N_ALU 重扫至 8 |
| 1377 | 107.3× | valu 1201 | **pre-offset**：idx 存绝对地址 → gather 免 addr-add（省 256 vop）|
| 1357 | 108.9× | valu 1181 | **L0 轮 idx 恒=forest_p → idx 更新省 MAC**；alu 组 stride-2 布局；L3 部分 select 打破 1363 平台 |
| 1321 | 111.8× | valu 1168 / 打包 | **删死 wrap**：level==height 轮所有元素必回卷到 0、且下一轮是 L0（L0 完全不读 idx）→ 该轮 idx 更新是**死代码**，整段省（连 wrap 都不算，省 128 vop）；顺带删无用常量、微调 L3SEL=30 |
| 1215 | 121.6× | valu 1168 / 头尾 drain | **消 drain（对角错位发射）**：group-major 下最后一组的 16 轮串行链在尾部独自 drain ~130 拍。改按对角 `r+SK*g` 错位发射 + 末 2 轮 round-major（各组独立单轮、还回填主体尾部空槽）→ body 打满、头尾空转从 ~150 砍到 ~50 拍 |
| 1188 | 124.4× | valu/alu/flow 三方均衡 | **idx「+c」搬 flow**：闲置的 flow（~55% 占用）接管 idx 更新的常量加 —— `c = parity? CFP+1 : CFP` 用 vselect 走 flow，valu/alu 各省一次加。四引擎（valu 1129 / alu 1121 / flow / load 1057）压到齐平，roofline 从 1168 降到 ~1121；重扫 IDXFLOW=20 / L3SEL=32 / SKEW=4 |
| 1163 | 127.0× | **head 序言（trace）** | **setup 地址搬离 flow**：逐拍 trace 看到 head 前 ~40 拍 **flow 满、alu/valu≈0**——是 46 条 `add_imm`（vbase=ivp+8g、rvec/Fvec=fvp+k）串在单槽 flow 上把计算全卡住。改用 alu 上的 **prefix-doubling** 生成这些地址序列（out[i]=out[i-s]+unit·s、log 层、只 1 条 const load），head 序言从 ~40 拍缩到 ~14 拍 |
| 1151 | 128.4× | 尾部 store drain（trace） | **store 打包**：trace 看到末尾 32 条 vstore 以 **1 拍 1 条** 串行（打包器对 mem 写保守 +1），拖出 ~18 拍纯 drain。放宽 store-store 到「只需不早于前一个 store」→ 同 bundle 可打包（异址互不影响、同址按程序序覆盖，golden 逐字节验证）→ 2/拍，省 ~9 拍 |
| 1143 | 129.3× | 尾部 store 堆积（trace） | **每组 store 各归一 region**：把每组 vstore 逐拍映射回 (组,轮) 看到——某慢组（末 alu 组 r15 到第 1140 拍才算完）的 store 把它**后面所有组的 store 全堵在 relative-order 后面**、堆到末尾。但 32 条写回地址两两不相交、本不必相互定序：给每组 store 各一个 region，谁 val 先好谁先写、与计算重叠 → body 从 40~1080 拍 **alu/valu 满 100%**，仅剩 head ~12 + tail ~12 |
| **1131** | **130.6×** | **hash 算子下界（critical-path）** | **阶段3 用 MAC 代 shl+add**：加了「依赖关键路径」分析——实测 critical-path 仅 247 拍 ≪ 1143 → 纯 **throughput 受限**（瓶颈 alu 1125），且 88% 的 alu 是 hash（算子分类分析）。唯一用**左移**的混合级 S3 `(b+C3)^(b<<9)`：把 +C3 折进 S2 的 MAC 常量，再把 `b<<9` 算成 MAC `b'*512 - C3*512` → S2+S3 从 4 个算子压成 3 个（每 hash 省 1 非-MAC）。100k 随机对拍 + golden 逐字节验证 |

**提交：全部 9 档通过**（correctness + `<147734`/`<18532`/`<2164`/`<1790`/`<1579`/`<1548`/`<1487`/`<1363`）。

**关键 insight「死代码消除」**：`level==height` 那轮（第 10 轮）所有元素同步到最深层、必全部回卷到 idx=0，
而紧接的第 11 轮是 L0（node_val 用常量广播、idx 更新 `A=parity+rvec(1)` 都**不读 idx**）——所以第 10 轮
辛苦算出的 idx（连同 wrap 的 cond/vselect）**从没被读过**，是死代码，直接删掉省 128 个 valu vop（1357→1329）。

**关键 insight「消 drain」（1321→1215）**：occupancy 逐拍看，主体（100~900 拍）valu 早已打满 6/6，
billed 比 roofline 高 13% 全卡在**头尾**：group-major 发射下，最后发射的那组要把自己 16 轮的串行链
（≈一条关键路径 ~130 拍）在尾部**独自 drain**（前面的组全做完了、没活可重叠），头部同理只有第一组在爬坡。
改成**对角错位发射**（wavefront `r+SK*g` 近似相等，各组错位在不同轮）让所有组一起推进、尾部不再单组独占；
再把**末 2 轮换成 round-major**（那几轮各组只剩独立单轮、彼此无依赖）——这些独立单轮既 drain 得快、又能
**回填主体尾部的空槽**。头部不铺开（round-major 头会让同层 gather 聚集、load 突发反更慢）。头尾空转 ~150→~50 拍。

**关键 insight「flow 第四引擎」（1215→1188）**：均衡到此，valu/alu 都近满、瓶颈是两者（~1121–1129），
而 **flow 引擎只 ~55% 占用**（只跑 select 的 vselect）。idx 更新的常量加 `A=2A+c`（c=parity+CFP）里的 `+c`
本来吃 valu/alu，改写成 `c = parity? CFP+1 : CFP` 的一条 **vselect 搬到 flow**（parity 已算好，两个常量向量
setup 时备好），valu/alu 每次省一次加。把负载摊到 valu/alu/flow **三方齐平**，roofline 从 1168 降到 ~1121。
flow 是单槽（1/拍），加太多会突发成新瓶颈，故只搬 IDXFLOW≈20 组、由扫参在四引擎间取平衡。

**关键 insight「逐拍 trace 挖 head/tail」（1188→1143）**：静态数 bundle 只知道「头尾没打满」，
`roofline.py --trace` 落逐拍波形、用 `insight` reader 读每拍每引擎的占用，才看清**成因**：
- **head 前 ~40 拍：flow 满、alu/valu≈0**——setup 的 46 条 `add_imm`（地址序列 ivp+8g / fvp+k）串在
  单槽 flow 上、把计算全卡住。地址是等差数列 → 改用 **alu 上的 prefix-doubling**（`out[i]=out[i-s]+unit·s`，
  s 每轮翻倍，log 层、只 1 条 const load 取步长），搬到 head 本就空闲的 alu，序言 ~40→~14 拍。
  （试过搬 alu 但立即数走常量池 load：把 head 的 load 面顶成瓶颈，更慢——**必须无 load 地算出来**。）
- **tail 末 ~18 拍：只有 store 在跑、且 1 拍 1 条**——打包器对内存写保守地逐个 +1 串行。但同 bundle 内多条
  store 落盘是按加入序（=程序序）、同址后写覆盖、异址互不影响，**与顺序语义一致**；故放宽 store-store 到
  「只需不早于前一个 store」即可同 bundle 打包 → 2/拍，末尾 drain 省 ~9 拍（golden 逐字节验证正确）。
- **store 仍在末尾堆积（1151→1143）**：打包后 store 变 2/拍，但把每条 vstore 映射回 (组,轮) 发现——它们
  仍被 relative-order 逼成组序，某慢组（末 alu 组 r15 到 1140 拍才算完）一堵、它后面所有组的 store 全压到
  末尾。而 32 条写回地址两两不相交、根本不必相互定序 → 给打包器加**内存 region** 机制、每组 store 各一个
  region，各 store 一到自己 val 就绪即发、与计算重叠，末尾只剩 1~2 拍。

**关键 insight「critical-path 确认 throughput 受限 + hash 算子下界」（1143→1131）**：给仿真增加**依赖
关键路径**分析（按 RAW+1/WAR/WAW/mem/barrier 规则建 DAG，算 ASAP）——实测 critical-path 仅 **247 拍**
≪ billed 1143 → **纯 throughput 受限**（非 latency），瓶颈是 alu 的 slot 吞吐（13493/12=1125）。再对 alu
算子**按用途分类**：88% 是 hash（xor/移位）。于是回头抠 hash 的算子数：6 个混合级里只有 **S3 用左移**
（`b<<9` = ×512 = 乘法），把它的 `+C3` 折进 S2 的 MAC 常量、`b<<9` 用 MAC `b'*512 - C3*512` 算，
S2+S3 从「MAC+shl+add+xor」4 个压成「MAC+MAC+xor」3 个。roofline 从 alu 1125 降到 valu 1078。
（S1/S5 用**右移**、非乘法，折不动；至此 hash 12 个算子是该 ISA 新下界。试过再平衡 valu/alu：alu 组的
idx 加/边际层比较搬 alu，都拉出跨引擎链或 flow 突发、伤打包，反不如不动——1131 = 1.05× roofline。）

至此仅剩 head（13 个 hash 常量在 load 面 2/拍 + broadcast + 第 0 轮起步的依赖链）+ tail（末组 r15 的深
hash drain，无后续可重叠）+ 少量 valu 中段小坑，都逼近结构下界，已难再压。

## 用 model 检查优化点的方法（贯穿全程）

1. `bridge.py` 守 golden（cpp==python 逐字节、billed 对齐）——每一步改完先验正确；
2. `roofline.py` 看**哪个引擎是瓶颈 + 下界**——驱动「减 load 还是减 compute / 往哪个引擎分流」的每一步决策：
   load-bound 时去重减 gather、group-major 消空转；compute-bound 时把 hash 分流到闲置 alu、pre-offset/L0
   特判减 vop、在 valu/alu 间平衡；
3. `--trace` + `trace_stats.py --gaps` 看**空转在哪**（load 4 大段=低层轮 → group-major；valu partial-fill
   → 平衡 N_ALU / 打破打包平台）；
4. 参数（`MAX_DEDUP` 去重到几层、`N_ALU` 几组走 alu、`L3SEL` 边际层多少组 select、alu 组布局）由 env
   扫参，按 roofline 在 load/valu/alu 间找最优——默认值即扫参得到的最优。

## 关键洞察小结

- **同步层级性质**：所有元素同步推进层级（都从 idx=0、每轮深一层、第 height 层同时回卷），故第 r 轮所有
  idx 在第 r%(height+1) 层、且是连续节点区间 → 低层可 setup 时把节点广播成向量、运行期用**线性 select**
  （只 2 临时）由 idx 选出 node_val，免 gather；wrap 只发生在一层、L0 轮 idx 恒定 → 大量特判省算子。
- **闲置引擎分流**：alu（12 槽）本空闲 → 把部分组的标量 hash 挪过去，valu+alu 合并吞吐 48→60 elem/拍；
  MAC 留 valu（1 槽=8elem，比标量 alu 的 16 槽密）、只非-MAC 分流 alu，避 2× 惩罚。
- **pre-offset**：idx 直接存绝对地址，gather 退化成一条 `load(nv, idx)`，省掉每次 gather 的地址加。
- **消 drain（对角错位发射）**：group-major 尾部单组独自 drain 一条关键路径；对角错位 + round-major 末轮
  让各组同步推进、尾部靠独立单轮回填 → 头尾空转 ~150→~50 拍（这一步是 1321→1215 的主因）。
- **flow 第四引擎**：valu/alu 都近满时，把 idx 的常量加改写成 vselect 搬到闲置的 flow，三引擎摊平（1215→1188）。
- **trace 挖 head/tail（1188→1143）**：逐拍波形定位三处成因 → ① prefix-doubling 生成 setup 地址搬离 flow
  （head 40→14 拍）；② 放宽 store-store 让 vstore 同 bundle 打包（2/拍）；③ 每组 store 各归一 region、免 relative-order
  把慢组后面的 store 全堵住（末尾 drain →1~2 拍）。
- **critical-path 分析**：给仿真加「依赖关键路径」测量，实测 247 ≪ billed → 全程 throughput 受限（非 latency），
  指明「减算子 = 减拍」，据此抠出 S3 的 MAC 折叠（1143→1131）。
- **S3 折叠（hash 算子从 13 降到 12）**：唯一用左移的混合级 `(b+C3)^(b<<9)` → 折 +C3 进 S2 常量、`b<<9` 用
  MAC 算，S2+S3 从 4 算子压成 3。S1/S5 用右移（非乘法）折不动 → 12 是该 ISA 新下界。
- **打包**：alu 整组标量化（链留同一引擎，散点会跨引擎卡顿）；布局/去重比例/发射错位/四引擎分流全由扫参平衡。
- **收尾在 1131**：throughput 受限、瓶颈 valu 1078；剩 head（hash 常量 load+broadcast 起步链）+ tail（末组 r15
  深 hash drain）+ 少量中段坑，都逼近结构下界。
- **试过但更差/无效**：① 二叉 mux 替线性 select（中间量共享池 → 组间 WAW 串行，1188→1266+）；② round-major
  头部铺开（同层 gather 突发 load）；③ schedule_list 关键路径调度（1724）；④ 惰性 vload（→1235）；⑤ store 插进
  尾部就地写（→1589）；⑥ setup 地址搬 alu 但立即数走常量池 load（把 head load 面顶成瓶颈，1178→1199）；
  ⑦ 单一 region 让 store 越过 gather（0 收益——真正 binding 是慢组 val 就绪 + relative-order；改**每组各一 region**
  才解，见上）；⑧ 纯对角尾（→1254+）；⑨ 步长常量前置发射（挤掉 hash 常量的 load 槽，反 →1161）；
  ⑩ S3 折叠后再平衡 valu/alu（alu 组 idx 加搬 flow →1182、alu 组边际层 gather →1150+、valu 组比较搬 alu →1141+、
  N_ALU 加到 10+ →1209+）——全因跨引擎链 / flow 突发 / load 突发伤打包，反不如不动；S3 折叠后 alu/valu 已难再平衡。
- **清死代码**：初始 IDX 广播（第 0 轮 L0 不读 idx 却写 idx → 死）与未使用的 forest_p 向量已删。

## 复现

```bash
cd logix && make
python bridge.py          # GOLDEN PASS：billed=1131, mism=0
python roofline.py        # 瓶颈 valu 1078（S3 折叠后）
cd .. && python tests/submission_tests.py   # 全 9 档 OK，CYCLES: 1131
git diff origin/main tests/                  # 空
# 逐拍 trace：cd logix && python roofline.py --trace vliw，再用 insight reader 读每拍每引擎占用
# 复扫参：MAX_DEDUP / N_ALU / L3SEL / IDXFLOW / EMIT / SKEW / TAILK 为 env 旋钮，默认值即最优
```
