# 结果：模型驱动把代码生成成绩从 147734 打到 1076（137.3×，过全部 9 档）

配套：[`DESIGN.md`](DESIGN.md)（建模）、[`PLAN.md`](PLAN.md)（计划）。

## 一句话

用 logix 建的忠实 C++ 性能模型 + roofline/trace/调度诊断/逐 bundle 解剖逐步定位瓶颈，把
`submission_tests.py` 的拍数从基线 **147734 → 1076（137.3×）**，全程对 `frozen_problem.Machine`
逐字节 `mism=0`、`tests/` 一字未改，**通过全部 9 个测试**（含 `<1363`，即超过 Readme 里
Opus 4.5 improved-harness 的 1363 与邮件门槛 1487）。最终 billed = 1.02× roofline，且
billed = load 收尾时刻（= load 下界 + 头空窗）+ 末组最后一轮 hash 的 10 拍——两项都在结构下限。

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
| 1131 | 130.6× | **hash 算子下界（critical-path）** | **阶段3 用 MAC 代 shl+add**：加了「依赖关键路径」分析——实测 critical-path 仅 247 拍 ≪ 1143 → 纯 **throughput 受限**（瓶颈 alu 1125），且 88% 的 alu 是 hash（算子分类分析）。唯一用**左移**的混合级 S3 `(b+C3)^(b<<9)`：把 +C3 折进 S2 的 MAC 常量，再把 `b<<9` 算成 MAC `b'*512 - C3*512` → S2+S3 从 4 个算子压成 3 个（每 hash 省 1 非-MAC）。100k 随机对拍 + golden 逐字节验证 |
| 1125 | 131.3× | valu 1078 / load 1052 双压线 | **线性插值 MAC 替代 select（pair-MAC）**：相邻两节点 (F[k],F[k+1]) 的 node_val 是绝对地址 A 的线性函数 `nv = A·D + E`（D=F[k+1]−F[k]、E=F[k]−(fp+k)·D，setup 用 head 空闲 alu 算好广播，mod 2³² 恒等）。L1 整层 1 条 MAC 顶掉 cmp+vselect；L2/L3 先用 vselect 在 pair 间选**系数向量** D/E 再 1 条 MAC——cmp 数从 2^L−1 砍到 2^(L−1)−1。valu −144、flow −192、alu −896 → roofline 1078→1053 |
| 1120 | 131.9× | head 序言（load 面） | **CONSTFLOW**：head 是 load-bound、flow 反而闲——常量隔一个改走 flow 的 `add_imm`（基于一个从不写入、恒为 0 的 scratch 词造任意 32 位常量），省下 head 的 load 槽给 vload |
| 1113 | 132.7× | load 的时间分布（前 100 拍空窗） | **EARLYW（早期 select 换回 gather）**：占用逐拍分析显示 load 从 ~100 拍起 100% 直到 ~1100——瓶颈是 load 的**时间分布**：前 ~100 拍 wavefront 还没推进到 gather 轮（r0-r3 全是 select），load 空转 ~119 槽。把最早两个 wavefront 组的 r1-r3 select 换**回** gather：+48 载入正好填进空窗，同时把 select 的 valu/flow 从最拥挤的前端撤走、前端推进更快。load 下界升到 1071 但 billed 降 7（1.04× roofline） |
| 1099 | 134.4× | 整个 r15 轮压在 load 流末端（逐 bundle 解剖） | **SEL15：死区广播 + MAC 链 select**。逐 bundle 解剖尾部发现：r15 全部排放在最后，其 256 个 gather 以 2/拍占满最后 ~130 拍——从饱和 load 流里任何位置减 8 载入都让终点提前 4 拍。把 6 个 alu 组的 r15 gather 换成 select：系数向量装不下（128 词）→ setup 只算 16 个 **D/E 标量**，尾部 r15 序里让 8 个 donor 组（valu 组）先排放、其 tmp/nv 死亡后**现场 vbroadcast** 出系数向量；select 用 **MAC 链**（`acc += cⱼ·(A·ΔDⱼ+ΔEⱼ)`，0 条 flow——尾部 flow 是墙、valu 反而空闲）。另修两处跨组串行：被转换组的 cond 槽按转换序号分（alu 组全是 g%4==0，原槽位全撞车）；尾段各轮排放序改「donor→被转换组→其余」（尾段按排放序在 frontier 排队执行） |
| 1095 | 134.9× | 末组终链（逐 bundle 解剖） | **TAILG：末组倒数第 2 轮的 L3 select 改回 gather**。终链 = g31 的 `r14结束→idx侧链(3)→gather(5)→hash(9)→store` 纯串行 18 拍；其 r14 的系数 select 是 4 级链，而终段 load 已空闲——改 gather 只 2 级。billed = 1.02× roofline（valu 1069） |
| 1094 | 135.0× | 终链的 idx 侧链 | TAILB：末 2 组的 idx 更新改 B 形式——`B = 2A+CFP` 只读 idx、在本轮 hash 期间提前算好（借宿 xor 后即空闲的 nv），`val→gather` 的侧链从「&→+c→MAC」3 拍缩成「&→add」2 拍；valu 算子数不变、纯时序重排。至此 billed = load 收尾（1081，已在下界）+ 末组终轮 hash 11 拍，无水分 |
| 1089 | 135.6× | hash 关键链 9 级（代数） | **u 并行 MAC**：S3 的 `u` 原来读 S2 的输出（两 MAC 串行）；代数上 `u = (v·33+C2C3)·512 − C3·512 = v·16896 + C2·512`，直接由 S1 输出算——两条 MAC 并行、每个 hash 链 9→8 级。终链 −1，头部各组爬坡每轮 −1 → 首批 gather 提早 ~4 拍缩 load 前窗 |
| 1082 | 136.5× | SEL15>7 的堆叠（广播墙） | **SEL15A：纯 ALU 形式的 r15 select**。valu 形式的转换受「系数广播要等 valu 尾部饱和解除」的墙限制（SEL15>7 必堆叠）；纯 ALU 形式逐 lane 用 setup 期就绪的 **l4de 标量**（乘/加/标量阈值 cmp 全走 alu，尾部 alu 空闲 30-50%）——零 valu、零广播、零死区依赖。3 个 valu 组转换（−24 load），valu 形式回调到 5；另 **L1SEL**（r1/r12 的 select 改 1 条 flow vselect——上一轮 L0 的 parity 恰好活在 tmp，−64 valu 降地板）与 **L0FOLD**（r11 的 `^F0` 折进 r10 的 S5 常量 `C5^F0`，−32 组·轮 ×1 算子）同步落地 |
| 1079 | 136.9× | body alu 低谷（trace 热图） | **SEL4A：r4 的 gather 也用纯 ALU select**。原来 r4 不能转（系数向量到尾部死区广播才有）——纯 ALU 形式不需要向量！候选组挑 r4 执行时刻恰落 body alu 低谷（b~650 ↔ g≈21，低谷源自 alu 组的 L0 轻轮）的组；1 组转换 −8 load、+296 alu 全被低谷吸收。第 2 组起低谷耗尽、每组 +25 拍（负收益，封顶 1） |
| 1078 | 137.0× | valu/load 双贴线的微平衡 | IDXFLOW 20→22：flow 尾部仍闲，再把 2 个 valu 组的 idx「+c」搬过去。至此 valu 6308（下界 1052）/ load 2079（1040）/ alu 12175（1015）、billed = 1.02× roofline；终点账目 = load 收尾 b1066 + 末组终轮 hash 10 拍 + store，无水分 |
| **1076** | **137.3×** | **首尾 pause 各独占一拍** | **pause 寄生注入**：打包器把 pause 当独占屏障；但 pause 与同 bundle 其余槽同拍执行、周期末才暂停，且 run 边界检查只读 inp_values——打包完成后把起始 pause 注入第一个 flow 空闲的 bundle、结束 pause 注入最后一个 bundle，各省 1 拍。另验证：再打包迭代（按落拍重排算子流重喂打包器）已收敛，1076 是贪心不动点；TGPRI/ALU_OFF/schedule_list 等发射序变体全部为负（load 队列不容插队） |

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

**关键 insight「pair-MAC 线性插值」（1131→1125）**：roofline 显示 valu 1078 独高（alu 954/flow 987 都闲）。
select 的本质是「从 2^L 个常量向量里按 idx 挑一个」——但相邻两个节点的值对绝对地址 A 是**线性**的：
`nv = A·D + E` 在 mod 2³² 下恒等（D/E 是 setup 算好的广播向量）。于是「挑 pair」用系数 select（cmp+vselect
减半）、「pair 内挑左右」直接一条 MAC 吃掉。valu/alu/flow 三方同时减负、算子净减 ~1200 个。

**关键 insight「run-ahead 毒化」（新增 sched_diff.py 工具）**：想把 nv/tmp 合并或组间共享临时区来腾
scratch（给 L4 系数），billed 反而 +45~+120。逐算子对比两版调度的 placement（`sched_diff.py`）发现：
贪心打包器高度依赖「后发组的早轮抢跑回填」（组 16 的 r0 自然落在 ~80 拍），而共享区让它在程序序上
排到先发组晚轮（~230 拍执行）之后，WAR/WAW 一锁、整条链连锁塌方。**任何跨组共享的 scratch，只要
「某组的晚轮」和「另一组的早轮」共槽，就会毒化 run-ahead**——按轮次分 bank 也只能救一半（alu 组
因 alu 引擎有余量会冲到最前端，生存期和谁都重叠）。这解释了此前多次「按理该赢却输」的重排尝试。

**关键 insight「load 的时间分布」（1120→1113）**：valu 1053 与 load 1052 双双压线后，逐拍占用显示
load 从 ~100 拍到 ~1100 拍 100% 满——billed ≈ (load 空转槽 + load 总槽)/2，**赢面全在把 load 的活
从「不得不晚」搬到「前 100 拍的空窗」**。r1-r3 的 select 换回 gather（EARLYW）对最早两个 wavefront
组生效：+48 载入填空窗、还把前端最挤的 valu/flow 让出来。反方向（SEL15/SEL4G 把 r15/r4 的 gather
换成 L4 select 以减 load 总量）则全线失败：L4 系数 select 是 7 层串行 flow 链，放尾部纯加延迟。

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
- **pair-MAC 线性插值（1131→1125）**：相邻两节点的 node_val 对绝对地址 A 线性（`nv = A·D + E`，mod 2³²
  恒等）→ L1 一条 MAC 免 select；L2/L3 在**系数向量**上做 select、值由末尾一条 MAC 算出，cmp 减半。
- **CONSTFLOW + EARLYW（1125→1113）**：head 常量隔一个走 flow `add_imm`（基于恒零 scratch 词）省 head
  load 槽；最早两个 wavefront 组的 r1-r3 select 换回 gather 填「前 100 拍 load 空窗」（load 的时间分布
  才是瓶颈：billed ≈ (load 空转 + load 总量)/2）。
- **1113 时的恒等式**：billed = load总量/2 + 头空窗/2 + 尾drain/2 = 1071 + 30 + 12。当时判「减 load
  总量需 ~143 词 scratch、无毒来源已穷尽」——后被**死区广播**破局（见下）：系数「向量」不必常驻，
  setup 存 16 个标量、尾部借 donor 组死掉的 tmp/nv 现场广播，只占 ~31 词。
- **死区广播（1113→1099 的钥匙）**：跨组共享 scratch 的毒在「借用方的早轮被锁到出借方晚轮之后」；
  但若借用发生在**程序序和执行序都晚于出借方全部生命期**的位置（尾部 r15 序：donor 先排放、
  广播注入、借用方后排放），共享就完全无毒。这是「回填全局化」判死配对共享后，唯一合法的
  scratch 复用形态——它救活了整条 L4-select 路线。
- **select 的形式要长成落点的形状**：同一个 16 节点 select，flow 链（cmp+vselect，每组 14 flow）
  放在尾部会撞 flow 单槽的墙（+85~200 全由此来）；MAC 链（`acc += cⱼ·Qⱼ`，15 MAC + 7 cmp、
  0 flow）放尾部恰好吃进空闲的 valu/alu。同理末组的终链上 load 空闲，gather（2 级）反而比
  任何 select（≥4 级）短（TAILG）。
- **四处跨组串行化的教训**（都由逐 bundle 解剖 + sched_diff 定位）：① 被转换组的 cond 共槽
  （alu 组全是 g%4==0）；② 尾段轮次按**排放序**在引擎 frontier 上排队——想早执行必须早排放
  （tgorder）；③ 双子预取的载入若在 r14 位置排放会在 load 队列插队、把全部 r15 gather 挤后
  （改为延迟到自己 r15 的排放点后仍无净收益——尾部 valu 和 load 一样满，无套利空间）；
  ④ 末组的 body 晚轮若把 cond 写进共享 t2a，会把所有同槽组的**尾段** r14 WAW 锁到它 ~b1040
  的执行点之后（程序序 body < tail，实测 +48）——凡「执行极晚的算子」都必须用专属临时区。
- **回填全局化（本轮最重要的负结果，sched_diff 实证）**：贪心打包器会把**任何组的早轮**回填进调度
  前端的空洞（组 17 的 r0 在无共享时排在第 86 拍！）——不是 wavefront 序附近的局部回填，而是全局的。
  推论：**任何跨组共享的 scratch 都必然耦合**「某组的晚轮」与「另一组的早轮」，共享即串行化。
  valu-only 配对 + 尾轮小池 + 池槽均衡修复后仍 +17 拍，判死。L4 select 的公平试验（SEL4G=7 中段
  alu 组，−4）被共享税吃掉，净收益为负——整条「腾 scratch 装 L4 系数」的路线到此关闭。
- **试过但更差/无效**：① 二叉 mux 替线性 select（中间量共享池 → 组间 WAW 串行，1188→1266+）；② round-major
  头部铺开（同层 gather 突发 load）；③ schedule_list 关键路径调度（1724）；④ 惰性 vload（→1235）；⑤ store 插进
  尾部就地写（→1589）；⑥ setup 地址搬 alu 但立即数走常量池 load（把 head load 面顶成瓶颈，1178→1199）；
  ⑦ 单一 region 让 store 越过 gather（0 收益——真正 binding 是慢组 val 就绪 + relative-order；改**每组各一 region**
  才解，见上）；⑧ 纯对角尾（→1254+）；⑨ 步长常量前置发射（挤掉 hash 常量的 load 槽，反 →1161）；
  ⑩ S3 折叠后再平衡 valu/alu（alu 组 idx 加搬 flow →1182、alu 组边际层 gather →1150+、valu 组比较搬 alu →1141+、
  N_ALU 加到 10+ →1209+）——全因跨引擎链 / flow 突发 / load 突发伤打包，反不如不动；S3 折叠后 alu/valu 已难再平衡；
  ⑪ nv/tmp 合并省 256 词（+120，run-ahead 毒化，见上）；⑫ (g,g+16) 配对共享 nv/tmp（+45，alu 组冲前端、
  尾轮被锁到搭档 body 之后——尾轮独立小池只救回一半）；⑬ L4 系数 select 卸载 r15/r4 的 gather（SEL15/SEL4G，
  +85~+200：7 层串行 flow 链放尾部纯加延迟、放中段 flow 突发）；⑭ 双子预取（不等 parity 把两个候选孩子都
  gather 回来，SPECG/SPEC15G，+4~+98：前端是公平推进不是抢跑，+8 载入/轮落进饱和区直接变拍数）；
  ⑮ 前 AE 轮全组走 alu（+33~+140，跨引擎交接 + 与 alu 组 r0 相撞）；
  ⑯ SHAREPAIR=2（仅 valu 组配对 + 尾轮小池 + 池槽均衡）仍 +17（回填全局化，见上）；
  ⑰ 非均匀尾斜率 TSG/TSK（后段组提前起跑）完全无感——发射序不决定节奏；
  ⑱ 小剂量双子预取（SPECG≤3×SPECR≤3、SPEC15G≤4）一律 +4~+16——前端是公平推进不是抢跑，
  额外载入落进饱和区直接变拍数。
- **顺手修的两个真 bug**：① `alu_groups=(i*stride)%ng` 在 N_ALU>8 时回绕重复、set 去重后实际组数
  缩水——此前 N_ALU=9/10 的所有扫参都是空转（改为均匀分布后实测 9/10 = 1115/1116，确认 8 最优）；
  ② 尾轮小池按 g%4 分槽，而 alu 组恰占满 g%4==0，valu 组被挤到 3 个槽上 8 深串行（改按共享组
  序号均衡，SHAREPAIR 税 1145→1130）。另：alu 组的 select 比较改用标量 fpp[j] 作阈值（标量比较
  不需要广播向量，L4 若启用可省 7 个 rvec）；setup 的 d/m/e 中间标量改 4 组轮转（45→12 词）。
- **清死代码**：初始 IDX 广播（第 0 轮 L0 不读 idx 却写 idx → 死）与未使用的 forest_p 向量已删。

## 理论下界：算力全部用满时最低多少拍（`lp_bound.py`）

把「还能不能更快」变成一个可解的线性规划：全部**语义强制**的工作量作常数、全部**已知交换手段**作变量，
问四个引擎同时用满时拍数 C 最小是多少。

- **工作量**（与 `inv.py` 算子清单逐项对齐，均已证不可再压）：hash = 512 组·轮 ×（4 MAC + 7 非 MAC）、
  nodeval xor 480（r11 已 L0FOLD 折掉）、idx 维护 448 组·轮（& + 2A-MAC + 可上 flow 的 +c）、
  L1/L2/L3 select、gather 2048 + setup load 55。
- **变量**：L4 gather→select 转换数 c4（每个 +15 MAC + 7 cmp、−8 load）；L1/L2/L3 反向转 gather；
  每类 flow-可选算子（idx +c、L0 sel、L1 sel、L2/L3 的 D/E 选择）留 flow 还是回落计算引擎
  （回落有溢价：valu 无 vselect，得用 MAC 链替）；多少算子从 valu 分流 alu（MAC 16 槽/条、非 MAC 8 槽/条）。
- **解**（`python lp_bound.py`）：**C = 1015.4，整数下界 1016 拍**，且是四约束同时绷紧的顶点：

  | 引擎 | LP 最优占用 | 利用率 |
  | - | - | - |
  | valu | 6,092 / 6,092 | 100.0% |
  | alu | 12,184 / 12,184 | 100.0% |
  | flow | 1,015 / 1,015 | 100.0% |
  | load | 2,031 / 2,031 | 100.0% |

**发现①：当前 kernel 的结构就是 LP 最优形状。** LP 选的 L4 转换数 c4=9 与实际的 SEL15=5+SEL15A=3+SEL4A=1=9
恰好一致；LP 分流 alu 的 1,508 条（12,184 槽）与 8 个 alu 组承担的 ~1,520 条重合；反向转换取 0 与实测
「转回 gather 全为负」一致。结构层面已无红利。

**发现②：实测 1076 与 1016 的 60 拍差，全部来自 LP 刻意忽略的三件事**：时间结构 ~25 拍（LP 假设任何算子
可在任何一拍执行；现实中 flow 的活只在 body 期存在、头尾必然空转，load 有头部依赖空窗）、头尾依赖链
~20 拍（首个 gather 前 14 拍爬坡 + 末组终轮 10 拍串行链）、整数粒度与打包摩擦 ~15 拍（alu 只能整组搬、
flow 卸载按类整批、贪心打包 ~1.5%）。

**结论口径**：纯吞吐下界（任何排布不可越过）= **1016**；加依赖与粒度后的现实地板 ≈ **1045–1060**；
当前 **1076** = 1.06× 理论下界；**950 在理论下界之下 66 拍，不可达**——到 950 需要 hash 每元素·轮降到
~9.5 算子（xor 与模 2³² 算术不可分配，已穷举折叠点）或 gather 每元素 <0.7 条 load（ISA 无此指令），
属于换 ISA 或换题的范畴。

## 复现

```bash
cd logix && make
python bridge.py          # GOLDEN PASS：billed=1076, mism=0
python lp_bound.py        # 理论下界 LP：C=1015.4（四引擎同时 100%）
python roofline.py        # 瓶颈 valu 1069（load 1052），1.02× roofline
cd .. && python tests/submission_tests.py   # 全 9 档 OK，CYCLES: 1076
git diff origin/main tests/                  # 空
# 逐拍 trace：cd logix && python roofline.py --trace vliw，再用 insight reader 读每拍每引擎占用
# 调度诊断：cd logix && python sched_diff.py A_KNOB=x -- B_KNOB=y   # 两组旋钮逐算子对比 placement
# 复扫参：MAX_DEDUP / N_ALU / L3SEL / IDXFLOW / EMIT / SKEW / TAILK / CONSTFLOW / EARLYW /
#   SEL15 / SEL15A / SEL4A / TAILG / TAILB / L4MAC / L1SEL / L0FOLD 为 env 旋钮，默认值即最优；SEL4G/SPECG/SPEC15G/SHAREPAIR/NVMERGE/
#   L4Q2/EARLY1G/TSG 是验证过为负收益或无感的机制，默认关（负结果清单见上）。
```
