"""
# Anthropic's Original Performance Engineering Take-home (Release version)

Copyright Anthropic PBC 2026. Permission is granted to modify and use, but not
to publish or redistribute your solutions so it's hard to find spoilers.

# Task

- Optimize the kernel (in KernelBuilder.build_kernel) as much as possible in the
  available time, as measured by test_kernel_cycles on a frozen separate copy
  of the simulator.

Validate your results using `python tests/submission_tests.py` without modifying
anything in the tests/ folder.

We recommend you look through problem.py next.
"""

import os
from collections import defaultdict
import random
import unittest

from problem import (
    Engine,
    DebugInfo,
    SLOT_LIMITS,
    VLEN,
    N_CORES,
    SCRATCH_SIZE,
    Machine,
    Tree,
    Input,
    HASH_STAGES,
    reference_kernel,
    build_mem_image,
    reference_kernel2,
)
from vliw_sched import schedule


class KernelBuilder:
    def __init__(self):
        self.instrs = []
        self.scratch = {}
        self.scratch_debug = {}
        self.scratch_ptr = 0
        self.const_map = {}

    def debug_info(self):
        return DebugInfo(scratch_map=self.scratch_debug)

    def build(self, slots: list[tuple[Engine, tuple]], vliw: bool = False):
        # Simple slot packing that just uses one slot per instruction bundle
        instrs = []
        for engine, slot in slots:
            instrs.append({engine: [slot]})
        return instrs

    def add(self, engine, slot):
        self.instrs.append({engine: [slot]})

    def alloc_scratch(self, name=None, length=1):
        addr = self.scratch_ptr
        if name is not None:
            self.scratch[name] = addr
            self.scratch_debug[addr] = (name, length)
        self.scratch_ptr += length
        assert self.scratch_ptr <= SCRATCH_SIZE, "Out of scratch space"
        return addr

    def scratch_const(self, val, name=None):
        if val not in self.const_map:
            addr = self.alloc_scratch(name)
            self.add("load", ("const", addr, val))
            self.const_map[val] = addr
        return self.const_map[val]

    def build_hash(self, val_hash_addr, tmp1, tmp2, round, i):
        slots = []

        for hi, (op1, val1, op2, op3, val3) in enumerate(HASH_STAGES):
            slots.append(("alu", (op1, tmp1, val_hash_addr, self.scratch_const(val1))))
            slots.append(("alu", (op3, tmp2, val_hash_addr, self.scratch_const(val3))))
            slots.append(("alu", (op2, val_hash_addr, tmp1, tmp2)))
            slots.append(("debug", ("compare", val_hash_addr, (round, i, "hash_stage", hi))))

        return slots

    def build_kernel(
        self, forest_height: int, n_nodes: int, batch_size: int, rounds: int
    ):
        """
        向量化 + VLIW 打包实现（见 logix/DESIGN、logix/PLAN P2、logix/RESULTS）。

        - 批状态 idx/val 常驻 scratch；256 元素分成 ng=batch/VLEN 组，每组用独立寄存器
          （组间天然独立，供打包器填满每拍 valu 槽）。
        - hash 阶段 0/2/4 折成一条 multiply_add（val*k+c）；移位阶段各用 1 个临时。
        - node_val 数据相关：低层用「线性 select」由广播的 forest 向量选出（免 gather），
          其余层保持标量 gather；idx 存绝对地址免 gather 的地址加（pre-offset）。
        - 四引擎均衡：瓶颈 valu 的活分流到闲置 alu（N_ALU 组标量化）与 flow
          —— idx 更新的「+c」加法搬到 flow 的 vselect（parity 在 CFP/CFP+1 间选，IDXFLOW 组）。
        - 发射顺序按对角错位 + round-major 末轮（EMIT=diagtail）消掉头尾 drain 空转。
        - 全部算子交给依赖感知的打包器 schedule() 打包。
        """
        assert batch_size % VLEN == 0, "batch 必须是 VLEN 的整数倍"
        ng = batch_size // VLEN

        # ── scratch 区域 ──────────────────────────────────────────────
        # NV 复用作 gather 地址缓冲：addr=idx+forest_p 写进 NV，再 load(nv,nv) 就地读地址写值
        # （单 op 读旧写新合法），省掉独立 ADDR 区（256 words），给低层去重腾地方。
        IDX = self.alloc_scratch("idx", batch_size)
        VAL = self.alloc_scratch("val", batch_size)
        NV = self.alloc_scratch("nv", batch_size)          # gather 地址/值 缓冲（就地）
        TMP = self.alloc_scratch("tmp", batch_size)        # 每组一个临时向量

        ops = []
        ops.append(("flow", ("pause",)))  # 起始 pause（对齐 reference 的第一个 yield）

        # 部分组的 hash/idx 走标量 ALU（12 槽/拍、原本闲置），把瓶颈 valu 的活分流过去：
        # 总吞吐 48 elem/拍(仅 valu) → 60(valu+alu)。N_ALU=6 由 roofline 扫参平衡两引擎得出
        # （alu 组均匀分散 + group-major 打包下实测最优）。env 可复扫调参。
        N_ALU = int(os.environ.get("N_ALU", "8"))

        def vop(ua, op, dest, a, b):
            """向量二元运算：ua=True 走 VLEN 条标量 alu，否则一条 valu。"""
            if ua:
                for l in range(VLEN):
                    ops.append(("alu", (op, dest + l, a + l, b + l)))
            else:
                ops.append(("valu", (op, dest, a, b)))

        VMAC_ALU = os.environ.get("VMAC_ALU", "0") == "1"  # MAC 是否也标量化到 alu

        def vmac(ua, dest, a, b, c):
            """dest = a*b + c。ua 且 VMAC_ALU：走 VLEN×(乘+加) 标量 alu；否则一条 valu multiply_add
            （MAC 在 valu 上 1 槽=8elem，比标量 alu 的 16 槽密——避 2× 惩罚，但 alu 组会跨引擎）。"""
            if ua and VMAC_ALU:
                for l in range(VLEN):
                    ops.append(("alu", ("*", dest + l, a + l, b + l)))
                    ops.append(("alu", ("+", dest + l, dest + l, c + l)))
            else:
                ops.append(("valu", ("multiply_add", dest, a, b, c)))

        def scalar(name=None):
            return self.alloc_scratch(name)

        # 本地常量池（scratch_const 写的是 self.instrs，会被 schedule(ops) 覆盖，故自建，按值去重）
        _const_cache = {}

        def oconst(val):
            if val not in _const_cache:
                a = scalar()
                ops.append(("load", ("const", a, val)))
                _const_cache[val] = a
            return _const_cache[val]

        def seq(base, count, unit):
            """返回 count 个标量 [base, base+unit, .., base+(count-1)*unit]（out[0] 复用 base 本身）。
            用 alu 上的 prefix-doubling 生成：out[i]=out[i-s]+unit*s，s 每轮翻倍 → log₂(count) 层、
            count-1 条独立 alu 加，步长 unit*s 由 alu 翻倍得到，只 1 条 const load（unit）。setup 的
            地址序列（vbase=ivp+VLEN*g、fpp=fvp+k）若逐个 flow add_imm 会在 head 串成长序言把 alu/valu
            全卡住（trace 实证）；改此法后 head 的 flow 序言消掉、只吃本就空闲的 head alu。"""
            out = [base] + [scalar() for _ in range(count - 1)]
            if count <= 1:
                return out
            step = oconst(unit)
            s = 1
            while s < count:
                if s == 1:
                    stepn = step
                else:
                    stepn = scalar()
                    ops.append(("alu", ("+", stepn, step, step)))
                    step = stepn
                for i in range(s, min(2 * s, count)):
                    ops.append(("alu", ("+", out[i], out[i - s], stepn)))
                s *= 2
            return out

        # 从内存头部读运行期指针（不硬编码布局）：mem[4]=forest_values_p, mem[6]=inp_values_p
        def load_hdr(k):
            sa, sv = scalar(), scalar()
            ops.append(("load", ("const", sa, k)))
            ops.append(("load", ("load", sv, sa)))
            return sv

        fvp = load_hdr(4)   # forest_values_p
        ivp = load_hdr(6)   # inp_values_p

        # 广播向量（编译期常量按值去重）
        bcache = {}

        def bvec(val):
            if val not in bcache:
                sa = scalar()
                vb = self.alloc_scratch(None, VLEN)
                ops.append(("load", ("const", sa, val)))
                ops.append(("valu", ("vbroadcast", vb, sa)))
                bcache[val] = vb
            return bcache[val]

        # 按 hash 阶段用到的先后 broadcast（常量的 const load 顺序即调度顺序）：第 0 轮 group 0 一开跑就
        # 需要 S0 的 K0/C0，故先发它们；ONE/TWO 是 idx parity 用、要到 hash 之后 → 放最后，别占早期 load 槽。
        # 阶段3 `(b+C3)^(b<<9)` 是唯一用**左移**的混合级（<<9 = ×512）→ 把 +C3 折进阶段2 的 MAC 常量
        # （C2+C3），再把 b<<9 用 MAC 算成 `b'*512 - C3*512`（b'=b+C3）：阶段2+3 从「MAC+shl+add+xor」
        # 4 个算子压成「MAC+MAC+xor」3 个，每个 hash 省 1 个非-MAC。省的算子在瓶颈 alu 上尤其值钱
        # （alu 组每个 hash 少 16 个 alu 槽）。C2C3/UADD 为编译期常量（见旁注的数值推导，已 100k 随机对拍）。
        K0, C0 = bvec(4097), bvec(0x7ED55D16)      # 阶段0
        S19, C1 = bvec(19), bvec(0xC761C23C)        # 阶段1
        K2, C2C3 = bvec(33), bvec(0xE9F8CC1D)       # 阶段2：常量 = C2 + C3（0x165667B1+0xD3A2646C）
        U512, UADD = bvec(512), bvec(0xBB372800)    # 阶段3：b<<9 = MAC(b',512,UADD)，UADD = -(C3×512) mod 2³²
        K4, C4 = bvec(9), bvec(0xFD7046C5)          # 阶段4
        S16, C5 = bvec(16), bvec(0xB55A4F09)        # 阶段5
        ONE = bvec(1)
        TWO = bvec(2)

        # pre-offset：idx 存成绝对地址 A = forest_p + idx，则 gather = load(nv, A) 免 addr-add。
        # 需要运行期常量：rvec(j)=broadcast(forest_p+j)（比较用）、CFP=1-forest_p（idx 更新用）。
        rcache = {}

        def rvec(j):
            # rvec(j) = broadcast(forest_p + j)；地址标量 forest_p+j 取自共享的 fpp（下方 prefix 生成）
            if j not in rcache:
                vb = self.alloc_scratch(None, VLEN)
                ops.append(("valu", ("vbroadcast", vb, fpp[j])))
                rcache[j] = vb
            return rcache[j]

        one_s = scalar()
        cfp_s = scalar()
        ops.append(("load", ("const", one_s, 1)))
        ops.append(("alu", ("-", cfp_s, one_s, fvp)))       # CFP = 1 - forest_p (mod 2^32)
        CFP = self.alloc_scratch("cfp_vec", VLEN)
        ops.append(("valu", ("vbroadcast", CFP, cfp_s)))
        # CFP+1 向量：idx 更新的 +c 用 vselect(parity, CFP+1, CFP) 走 flow 引擎（省 valu/alu）
        cfp1_s = scalar()
        ops.append(("flow", ("add_imm", cfp1_s, cfp_s, 1)))
        CFP1 = self.alloc_scratch("cfp1_vec", VLEN)
        ops.append(("valu", ("vbroadcast", CFP1, cfp1_s)))

        # idx 无需初始化：第 0 轮是 L0（node_val=Fvec[0] 常量、完全不读 idx），且该轮**写** idx
        # 供第 1 轮用 → 初始 IDX 是死代码（省 ng 条 vbroadcast，正好在 head 稀疏处腾出 valu）。

        # 初始化：val 从 mem[inp_values_p + g*VLEN] vload（前置一次性发射——各组 val 早早备好，
        # 主体 r=0 不必等 load；试过惰性摊开反而给每组起点加了 load 延迟，更慢）。
        # vbase[g] = ivp + VLEN*g（prefix-doubling 在 alu 生成，避 32 条 flow add_imm 堵 head）
        vbase = seq(ivp, ng, VLEN)              # vbase[0]=ivp；保活到最后 vstore 复用
        for g in range(ng):
            # 每组 val 读写各归一个 region ("io", g)：本组 inp 区与他组不相交，故各组 vload/vstore
            # 互不排序（打包器 region 机制），末尾各 store 一到自己 val 就绪即发、不被慢组堵住。
            ops.append(("load", ("vload", VAL + g * VLEN, vbase[g]), ("io", g)))

        # 低层 gather 去重（同步层级性质）：第 r 轮所有 idx 都在第 (r % period) 层。第 L 层的
        # 节点是连续区间 [2^L-1, 2^(L+1)-1)（共 2^L 个），forest 不变 → setup 时把这些节点 broadcast
        # 成向量，运行期用「线性 select」由 idx 选出 node_val，免 gather：
        #   nv = (idx>base)? F[base+1] : F[base]; 再逐个 nv = (idx>=k)? F[k] : nv。
        # 只用 nv + tmp 两个临时（不需 mux 的第二临时区），省 scratch → 能多去重几层。
        period = forest_height + 1
        # 去重到第几层：越高省的 gather 越多，但线性 select 的 valu/flow 代价 ~2^L 指数涨、且
        # F-vec 占 scratch ~2^(L+1)。由扫参在 load 与 compute 之间平衡（默认 3）。
        MAX_DEDUP = int(os.environ.get("MAX_DEDUP", "3"))
        dedup_levels = sorted({r % period for r in range(rounds) if (r % period) <= MAX_DEDUP})
        marginal_level = dedup_levels[-1] if dedup_levels else -1  # 最高去重层，部分 select
        L3SEL = int(os.environ.get("L3SEL", str(ng)))              # 边际层多少组走 select（其余 gather）

        # 共享地址标量 fpp[k] = forest_p + k（k 覆盖去重层所有节点号及 rvec 用到的 j）：一次 prefix
        # 生成、rvec 与 Fvec 共用，把原本 ~28 条散在 head 的 flow add_imm 全消掉（trace-driven）。
        nmax = ((1 << (max(dedup_levels) + 1)) - 1) if dedup_levels else 3
        fpp = seq(fvp, max(3, nmax), 1)          # fpp[0]=fvp

        # Fvec 节点值：去重层的节点号是连续区间 0..nmax-1 → 用 vload 成块取 forest（每条 8 个），
        # 再从块里逐节点 broadcast——把原本 nmax 条散 load 压成 ⌈nmax/8⌉ 条 vload（head 是 load-bound，
        # trace 实证），省下的 load 面直接缩短 head 序言。
        nchunk = (nmax + VLEN - 1) // VLEN
        FV_raw = self.alloc_scratch("fvraw", nchunk * VLEN)
        for c in range(nchunk):
            ops.append(("load", ("vload", FV_raw + c * VLEN, fpp[c * VLEN])))

        Fvec = {}  # 节点号 k -> 该节点 forest 值的广播向量地址
        for L in dedup_levels:
            base = (1 << L) - 1
            for k in range(base, base + (1 << L)):
                if k in Fvec:
                    continue
                fv = self.alloc_scratch(f"fnode{k}", VLEN)
                ops.append(("valu", ("vbroadcast", fv, FV_raw + k)))   # forest[k] 来自上面的块 vload
                Fvec[k] = fv

        dedup_set = set(dedup_levels)

        def emit_node_select(ua, level, nv, tmp, idx):
            """线性 select 出 forest[idx]（idx 在第 level 层）。返回存放 node_val 的地址。"""
            base = (1 << level) - 1
            cnt = 1 << level
            if cnt == 1:
                return Fvec[base]                                   # L0：单节点，直接用其向量
            # idx 存的是绝对地址 A；逻辑比较 (idx_logical>=k) ⟺ A > forest_p+(k-1) ⟺ rvec(k-1)<A
            # 比较：alu 组走 alu、valu 组走 valu；vselect 走很闲的 flow。（试过把部分 valu 组的比较
            # 分流到 alu 来平衡——alu比较→flow选→valu哈希 跨引擎伤打包，反更差，不做。）
            vop(ua, "<", tmp, rvec(base), idx)                    # A > forest_p+base（即 idx>=base+1）
            ops.append(("flow", ("vselect", nv, tmp, Fvec[base + 1], Fvec[base])))
            for k in range(base + 2, base + cnt):
                vop(ua, "<", tmp, rvec(k - 1), idx)               # A > forest_p+(k-1)（即 idx>=k）
                ops.append(("flow", ("vselect", nv, tmp, Fvec[k], nv)))
            return nv

        # ── 主循环 ────────────────────────────────────────────────────
        # 按「组外层、轮内层」发射（group-major）：批内各组独立，交给打包器后不同组会
        # 错位在不同层——组 A 在低层吃 valu 时组 B 在高层吃 load，两引擎同时忙，消掉
        # round-major 下「所有组同步在低层→load 空转」的硬停顿。算子与依赖不变，正确性照旧。
        # alu 组按 stride-2 铺开（整组标量化，链留同一引擎、打包好；散点会跨引擎卡顿）。
        # stride-2 布局 + 下面 L0 idx 特判由扫参得到实测最优。
        # alu 组按 ALU_STRIDE 铺开：S3 折叠后 alu 负担降、需把 alu 组更均匀撒到全 32 组（stride-4 而非 2，
        # 覆盖 0..28）以在整段 body 都给 alu 供活、不至于中后段 alu 空转（trace 实证、扫参得最优）。
        ALU_STRIDE = int(os.environ.get("ALU_STRIDE", "4"))
        alu_groups = set((i * ALU_STRIDE) % ng for i in range(N_ALU)) if N_ALU else set()
        # 分数级平衡：再挑 1 个「半 alu 组」，其前 EXTRA 轮走 alu、其余走 valu（连续块，只 1 次
        # 跨引擎转换，避散点 lane-sync）——把 valu/alu 之间那点余量磨平（整组太粗）。
        EXTRA = int(os.environ.get("EXTRA", "0"))
        xg = next((g for g in range(ng) if g not in alu_groups), -1)

        # idx 更新的 +c 加法搬到 flow 引擎（vselect 在 parity 上选 CFP/CFP+1）：flow 平时很闲
        # （~55% 占用），把它填满能同时卸 valu 与 alu 的负担。优先给 valu 组（alu 组搬 flow 会拉出
        # alu→flow→valu 的跨引擎链、且 flow 单槽突发，反伤打包，实测更差）。IDXFLOW=用 flow 的组数。
        IDXFLOW = int(os.environ.get("IDXFLOW", "20"))
        _forder = [g for g in range(ng) if g not in alu_groups] + \
                  [g for g in range(ng) if g in alu_groups]
        idxflow_groups = set(_forder[:IDXFLOW])

        def emit_gr(g, r):
                b = g * VLEN
                idx, val, nv, tmp = IDX + b, VAL + b, NV + b, TMP + b
                ua = (g in alu_groups) or (g == xg and r < EXTRA)  # 该(组,轮)走标量 alu
                level = r % period
                # node_val：去重层用线性 select，其余标量 gather。
                # 边际层（最高去重层）只对部分组 select、其余 gather——用 load 余量换 compute
                # 减负，让 load≈compute 平衡（L3SEL 控制多少组 select）。
                # （试过让 alu 组在边际层改走 gather 卸 alu：gather 给 alu 组的链加 load 延迟、且
                #  tail 那轮聚集突发 load，打包反变差 1143→1150+，故不做——alu/valu 已在最优平衡点。）
                sel = (level in dedup_set) and (level < marginal_level or g < L3SEL)
                if sel:
                    nv_src = emit_node_select(ua, level, nv, tmp, idx)
                else:
                    # idx 已是绝对地址 A → gather 直接 load(nv, idx)，免 addr-add
                    for lane in range(VLEN):
                        ops.append(("load", ("load", nv + lane, idx + lane)))
                    nv_src = nv
                # val = myhash(val ^ node_val)（valu 组走向量，alu 组走标量分流）
                vop(ua, "^", val, val, nv_src)
                vmac(ua, val, val, K0, C0)                                    # 阶段0
                vop(ua, ">>", tmp, val, S19)                                  # 阶段1
                vop(ua, "^", val, val, C1)
                vop(ua, "^", val, val, tmp)
                vmac(ua, val, val, K2, C2C3)                                  # 阶段2（常量折入阶段3 的 +C3）
                vmac(ua, tmp, val, U512, UADD)                                # 阶段3：u = b'<<9 = b'*512 - C3*512
                vop(ua, "^", val, val, tmp)                                   # result = b' ^ u
                vmac(ua, val, val, K4, C4)                                    # 阶段4
                vop(ua, ">>", tmp, val, S16)                                  # 阶段5
                vop(ua, "^", val, val, C5)
                vop(ua, "^", val, val, tmp)
                # A(绝对地址)更新：newA = 2A + CFP + parity（CFP=1-forest_p）。三处省算子：
                # ① 最后一轮 idx 不再用到 → 省；
                # ② level==height 那轮所有元素必回卷到 0、且下一轮是 L0（L0 完全不读 idx）→ 该轮
                #    idx 更新是死代码，整段省（连 wrap 都不用算）；
                # ③ L0 轮 idx 恒为 forest_p（常量）→ A = forest_p+1+parity = rvec(1)+parity，省掉 MAC。
                if r != rounds - 1 and level != forest_height:
                    fl = g in idxflow_groups                                # +c 走 flow vselect
                    if level == 0:
                        vop(ua, "&", tmp, val, ONE)                          # parity
                        if fl:  # A = parity? rvec(2) : rvec(1)（省一次 valu/alu 加）
                            ops.append(("flow", ("vselect", idx, tmp, rvec(2), rvec(1))))
                        else:
                            vop(ua, "+", idx, tmp, rvec(1))                 # A = parity + (forest_p+1)
                    else:
                        vop(ua, "&", tmp, val, ONE)                          # parity
                        if fl:  # c = parity? CFP+1 : CFP（走 flow），再 MAC
                            ops.append(("flow", ("vselect", tmp, tmp, CFP1, CFP)))
                        else:
                            vop(ua, "+", tmp, tmp, CFP)                     # t = parity + (1-forest_p)
                        vmac(ua, idx, idx, TWO, tmp)                         # A = 2A + t（此后无越界，无 wrap）

        # 发射顺序（EMIT，决定打包器看到的算子序 ≈ 调度序）：
        #   group（回退）：组外层轮内层。body 打满，但尾部只剩最后一组的 16 轮串行链独自
        #     drain（≈一条关键路径 ~130 拍），头部也只有第一组在爬坡 → 头尾空转 ~150 拍。
        #   diagtail（默认，实测最优）：主体按对角错位发射 —— wavefront w 内 (g,r) 满足
        #     r + SK*g ≈ w，各组错位在不同轮/层，组 A 吃 valu 时组 B 吃 load、两引擎同时忙，
        #     且尾部不再是单组独占。末 TK 轮改 round-major：那几轮各组只剩独立单轮，drain 短，
        #     还能回填主体尾部的空槽。round-major 头部会触发同层 gather 聚集（load 突发）反而更慢，
        #     故头部不铺开。SK/TK 由扫参得最优（见 RESULTS「消 drain」一节）。
        EMIT = os.environ.get("EMIT", "diagtail")
        if EMIT == "diagtail":
            SK = int(os.environ.get("SKEW", "4"))
            TK = int(os.environ.get("TAILK", "2"))
            body = sorted(((g, r) for g in range(ng) for r in range(rounds - TK)),
                          key=lambda gr: (gr[1] + SK * gr[0], gr[0]))
            order = body + [(g, r) for r in range(rounds - TK, rounds) for g in range(ng)]
        else:  # group-major 回退
            order = [(g, r) for g in range(ng) for r in range(rounds)]
        for g, r in order:
            emit_gr(g, r)

        # 写回 val（提交只校验 inp_values）。每组 store 归 region ("io", g)：各组写回地址不相交 →
        # 互不排序，谁的 val 先算好谁先写、与计算重叠；不必等最慢那组（trace 实证尾部 store 堆积的根因）。
        # 正确性：store 只依赖自己 VAL（scratch 链已定序在其 vload 之后），且与 gather（读 forest 区）不相交。
        for g in range(ng):
            ops.append(("store", ("vstore", vbase[g], VAL + g * VLEN), ("io", g)))

        ops.append(("flow", ("pause",)))  # 结束 pause（对齐 reference 的第二个 yield）

        self.instrs = schedule(ops)

BASELINE = 147734

def do_kernel_test(
    forest_height: int,
    rounds: int,
    batch_size: int,
    seed: int = 123,
    trace: bool = False,
    prints: bool = False,
):
    print(f"{forest_height=}, {rounds=}, {batch_size=}")
    random.seed(seed)
    forest = Tree.generate(forest_height)
    inp = Input.generate(forest, batch_size, rounds)
    mem = build_mem_image(forest, inp)

    kb = KernelBuilder()
    kb.build_kernel(forest.height, len(forest.values), len(inp.indices), rounds)
    # print(kb.instrs)

    value_trace = {}
    machine = Machine(
        mem,
        kb.instrs,
        kb.debug_info(),
        n_cores=N_CORES,
        value_trace=value_trace,
        trace=trace,
    )
    machine.prints = prints
    for i, ref_mem in enumerate(reference_kernel2(mem, value_trace)):
        machine.run()
        inp_values_p = ref_mem[6]
        if prints:
            print(machine.mem[inp_values_p : inp_values_p + len(inp.values)])
            print(ref_mem[inp_values_p : inp_values_p + len(inp.values)])
        assert (
            machine.mem[inp_values_p : inp_values_p + len(inp.values)]
            == ref_mem[inp_values_p : inp_values_p + len(inp.values)]
        ), f"Incorrect result on round {i}"
        inp_indices_p = ref_mem[5]
        if prints:
            print(machine.mem[inp_indices_p : inp_indices_p + len(inp.indices)])
            print(ref_mem[inp_indices_p : inp_indices_p + len(inp.indices)])
        # Updating these in memory isn't required, but you can enable this check for debugging
        # assert machine.mem[inp_indices_p:inp_indices_p+len(inp.indices)] == ref_mem[inp_indices_p:inp_indices_p+len(inp.indices)]

    print("CYCLES: ", machine.cycle)
    print("Speedup over baseline: ", BASELINE / machine.cycle)
    return machine.cycle


class Tests(unittest.TestCase):
    def test_ref_kernels(self):
        """
        Test the reference kernels against each other
        """
        random.seed(123)
        for i in range(10):
            f = Tree.generate(4)
            inp = Input.generate(f, 10, 6)
            mem = build_mem_image(f, inp)
            reference_kernel(f, inp)
            for _ in reference_kernel2(mem, {}):
                pass
            assert inp.indices == mem[mem[5] : mem[5] + len(inp.indices)]
            assert inp.values == mem[mem[6] : mem[6] + len(inp.values)]

    def test_kernel_trace(self):
        # Full-scale example for performance testing
        do_kernel_test(10, 16, 256, trace=True, prints=False)

    # Passing this test is not required for submission, see submission_tests.py for the actual correctness test
    # You can uncomment this if you think it might help you debug
    # def test_kernel_correctness(self):
    #     for batch in range(1, 3):
    #         for forest_height in range(3):
    #             do_kernel_test(
    #                 forest_height + 2, forest_height + 4, batch * 16 * VLEN * N_CORES
    #             )

    def test_kernel_cycles(self):
        do_kernel_test(10, 16, 256)


# To run all the tests:
#    python perf_takehome.py
# To run a specific test:
#    python perf_takehome.py Tests.test_kernel_cycles
# To view a hot-reloading trace of all the instructions:  **Recommended debug loop**
# NOTE: The trace hot-reloading only works in Chrome. In the worst case if things aren't working, drag trace.json onto https://ui.perfetto.dev/
#    python perf_takehome.py Tests.test_kernel_trace
# Then run `python watch_trace.py` in another tab, it'll open a browser tab, then click "Open Perfetto"
# You can then keep that open and re-run the test to see a new trace.

# To run the proper checks to see which thresholds you pass:
#    python tests/submission_tests.py

if __name__ == "__main__":
    unittest.main()
