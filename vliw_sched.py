"""
依赖感知的 VLIW 列表调度器（打包器）。

把一条线性算子流 [(engine, slot), ...] 贪心打包成 bundle 列表，逼近 roofline。
遵守 problem.py 的时序不变量（见 logix/DESIGN §1.3）：

  - 周期末统一提交：同 bundle 内所有 slot 读旧值、写下拍才可见。
  - RAW（读依赖写）：消费者必须在生产者之后 **≥1 个 bundle**。
  - WAR（写依赖读）：写可以和读 **同 bundle**（读者读旧值）。
  - WAW（写依赖写）：保守隔 1 个 bundle（避免同 bundle 双写同址的次序歧义）。
  - 内存：gather(load) 是对 forest 的纯读、可并行；store 保守串行（load 在 store 后 +1）。
  - 控制算子（pause/halt/jump...）作硬屏障：其前后算子不得跨越。

对齐 SLOT_LIMITS 每拍每引擎上限。调度对任意算子流都正确，速度取决于算子流本身的并行度
（地址复用越少越好 → 配合 SSA 式临时变量最优）。
"""
from problem import VLEN, SLOT_LIMITS

_BARRIER_OPS = {"pause", "halt", "jump", "cond_jump", "cond_jump_rel", "jump_indirect"}


def slot_rw(engine, slot):
    """返回 (reads:set, writes:set, mem_read:bool, mem_write:bool, barrier:bool)。"""
    op = slot[0]
    a = slot[1:]
    reads, writes = set(), set()
    mem_read = mem_write = barrier = False

    if engine == "alu":
        dest, a1, a2 = a
        reads = {a1, a2}
        writes = {dest}
    elif engine == "valu":
        if op == "vbroadcast":
            dest, src = a
            reads = {src}
            writes = set(range(dest, dest + VLEN))
        elif op == "multiply_add":
            dest, x, y, z = a
            reads = set(range(x, x + VLEN)) | set(range(y, y + VLEN)) | set(range(z, z + VLEN))
            writes = set(range(dest, dest + VLEN))
        else:  # 逐元素
            dest, a1, a2 = a
            reads = set(range(a1, a1 + VLEN)) | set(range(a2, a2 + VLEN))
            writes = set(range(dest, dest + VLEN))
    elif engine == "load":
        if op == "const":
            dest, _val = a
            writes = {dest}
        elif op == "load":
            dest, addr = a
            reads = {addr}
            writes = {dest}
            mem_read = True
        elif op == "load_offset":
            dest, addr, off = a
            reads = {addr + off}
            writes = {dest + off}
            mem_read = True
        elif op == "vload":
            dest, addr = a
            reads = {addr}
            writes = set(range(dest, dest + VLEN))
            mem_read = True
        else:
            raise ValueError(f"unknown load op {op}")
    elif engine == "store":
        if op == "store":
            addr, src = a
            reads = {addr, src}
            mem_write = True
        elif op == "vstore":
            addr, src = a
            reads = {addr} | set(range(src, src + VLEN))
            mem_write = True
        else:
            raise ValueError(f"unknown store op {op}")
    elif engine == "flow":
        if op == "select":
            dest, cond, x, y = a
            reads = {cond, x, y}
            writes = {dest}
        elif op == "vselect":
            dest, cond, x, y = a
            reads = set(range(cond, cond + VLEN)) | set(range(x, x + VLEN)) | set(range(y, y + VLEN))
            writes = set(range(dest, dest + VLEN))
        elif op == "add_imm":
            dest, x, _imm = a
            reads = {x}
            writes = {dest}
        elif op == "coreid":
            writes = {a[0]}
        elif op == "trace_write":
            reads = {a[0]}
        elif op in _BARRIER_OPS:
            barrier = True
        else:
            raise ValueError(f"unknown flow op {op}")
    elif engine == "debug":
        pass  # 调用方应已剔除
    else:
        raise ValueError(f"unknown engine {engine}")
    return reads, writes, mem_read, mem_write, barrier


def schedule(ops):
    """ops: list[(engine, slot)] 或 (engine, slot, region)（不含 debug）。返回 list[dict engine->[slot,...]]。

    内存别名分区（region）：同一 region 内的 load/store 按保守内存序排（可能别名）；不同 region 的
    内存算子**互不排序**（调用方保证它们访问不相交的内存区）。默认 region=None（单一共享区，等价旧行为）。
    本 kernel 给**每组的 vstore 各一个 region**：32 条写回地址两两不相交，本不必相互串行——否则某组
    val 算得晚会把它后面所有组的 store 全堵住（trace 实证的尾部 store 堆积）。各 store 一到自己 val
    就绪即可发，与计算重叠。正确性：每组 store 只依赖自己的 VAL（scratch 链已定序到其 vload 之后）。
    """
    bundles = []          # index -> {engine: [slot,...]}
    counts = []           # index -> {engine: n}
    last_write = {}       # addr -> bundle idx
    last_read = {}        # addr -> bundle idx
    last_mem_write = {}   # region -> bundle idx
    last_mem_read = {}    # region -> bundle idx
    barrier_at = -1
    max_used = -1
    # 每引擎「最小的仍有空槽的 bundle」：只前进不后退（低于它的 bundle 对该引擎已满）。
    # 这样后来的独立算子能回填前面 bundle 的空槽（打满 valu/load），同时保持近线性。
    first_free = {e: 0 for e in SLOT_LIMITS}

    def ensure(idx):
        while len(bundles) <= idx:
            bundles.append({})
            counts.append({})

    for op in ops:
        engine, slot = op[0], op[1]
        region = op[2] if len(op) > 2 else None
        reads, writes, mr, mw, barrier = slot_rw(engine, slot)

        if barrier:
            # 硬屏障：放在所有已排算子之后，独占一拍；其后算子不得跨越。
            idx = max(max_used + 1, barrier_at + 1)
            ensure(idx)
            bundles[idx].setdefault(engine, []).append(slot)
            counts[idx][engine] = counts[idx].get(engine, 0) + 1
            barrier_at = idx
            max_used = max(max_used, idx)
            continue

        earliest = barrier_at + 1
        for r in reads:
            w = last_write.get(r, -1)
            if w + 1 > earliest:
                earliest = w + 1
        for w in writes:
            rd = last_read.get(w, -1)
            if rd > earliest:
                earliest = rd
            pw = last_write.get(w, -1)
            if pw + 1 > earliest:
                earliest = pw + 1
        lmw = last_mem_write.get(region, -1)
        lmr = last_mem_read.get(region, -1)
        if mr and lmw + 1 > earliest:
            earliest = lmw + 1                  # RAW：load 读前面 store 写的新值 → 隔 1 个 bundle
        if mw:
            if lmr > earliest:
                earliest = lmr                  # WAR：store 不早于前面的 load（同 bundle 可，读旧写新）
            if lmw > earliest:
                # store-store 只需保持相对次序（不早于前一个 store），允许**同 bundle 打包**：
                # 同 bundle 内的 mem_write 按加入序（=程序序）落盘，同址后写覆盖前写 → 与顺序语义一致，
                # 异址互不影响。故不必逐个 +1 串行（那样 32 条 vstore 要 32 拍、末尾纯 drain）。
                earliest = lmw

        limit = SLOT_LIMITS[engine]
        # 从 max(earliest, first_free) 起找第一个该引擎有空槽的 bundle
        idx = earliest if earliest > first_free[engine] else first_free[engine]
        ensure(idx)
        while counts[idx].get(engine, 0) >= limit:
            idx += 1
            ensure(idx)
        # 更新 first_free：若刚填的是当前最小空槽 bundle，往前推到下一个未满的
        if idx == first_free[engine] and counts[idx].get(engine, 0) + 1 >= limit:
            ff = idx + 1
            ensure(ff)
            while counts[ff].get(engine, 0) >= limit:
                ff += 1
                ensure(ff)
            first_free[engine] = ff

        bundles[idx].setdefault(engine, []).append(slot)
        counts[idx][engine] = counts[idx].get(engine, 0) + 1

        for w in writes:
            last_write[w] = idx
        for r in reads:
            if idx > last_read.get(r, -1):
                last_read[r] = idx
        if mr and idx > last_mem_read.get(region, -1):
            last_mem_read[region] = idx
        if mw and idx > last_mem_write.get(region, -1):
            last_mem_write[region] = idx
        if idx > max_used:
            max_used = idx

    return [b for b in bundles if b]  # 丢掉空 bundle（不影响计费/正确性，前提：无绝对跳转目标）


def schedule_list(ops):
    """cycle-by-cycle 列表调度：按关键路径高度优先填槽，逼近 roofline。

    与 schedule() 同一套时序约束（RAW +1 / WAR 0 / WAW +1 / mem / barrier），但不按程序序
    贪心，而是构 DAG 后逐拍从「就绪表」里挑高度最大的算子填满每个引擎的槽——独立算子能更早
    上位、跨轮/跨组重叠更满。正确性仍由 golden 兜底。
    """
    from heapq import heappush, heappop

    n = len(ops)
    rw = [slot_rw(e, s) for (e, s) in ops]
    preds = [[] for _ in range(n)]        # v -> [(u, gap)]
    succ = [[] for _ in range(n)]

    writer = {}          # addr -> 最近写它的 op
    readers = {}         # addr -> 上次写之后读它的 op 集合
    mem_writer = -1
    mem_readers = set()
    last_barrier = -1

    for i in range(n):
        reads, writes, mr, mw, barrier = rw[i]
        e = []
        if barrier:
            e = [(u, 1) for u in range(last_barrier + 1, i)]  # 屏障：其前所有算子
            preds[i] = e
            for (u, _g) in e:
                succ[u].append(i)
            last_barrier = i
            continue
        if last_barrier >= 0:
            e.append((last_barrier, 1))
        for r in reads:
            w = writer.get(r, -1)
            if w >= 0:
                e.append((w, 1))                      # RAW
        for w in writes:
            for rd in readers.get(w, ()):             # WAR（同拍可）
                e.append((rd, 0))
            pw = writer.get(w, -1)
            if pw >= 0:
                e.append((pw, 1))                     # WAW
        if mr and mem_writer >= 0:
            e.append((mem_writer, 1))
        if mw:
            for rd in mem_readers:
                e.append((rd, 0))
            if mem_writer >= 0:
                e.append((mem_writer, 1))
        preds[i] = e
        for (u, _g) in e:
            succ[u].append(i)
        for w in writes:
            writer[w] = i
            readers[w] = set()
        for r in reads:
            readers.setdefault(r, set()).add(i)
        if mr:
            mem_readers.add(i)
        if mw:
            mem_writer = i
            mem_readers = set()

    # 关键路径高度（到汇点的最长 op 数），逆序用 succ（i 的后继编号恒大于 i）
    height = [1] * n
    for v in range(n - 1, -1, -1):
        h = 0
        for w in succ[v]:
            if height[w] > h:
                h = height[w]
        height[v] = h + 1

    sched = [-1] * n
    remaining = [len(preds[i]) for i in range(n)]
    waiting = []                  # (earliest_bundle, -height, op)
    for i in range(n):
        if remaining[i] == 0:
            heappush(waiting, (0, -height[i], i))

    bundles = []
    counts = []

    def ensure(idx):
        while len(bundles) <= idx:
            bundles.append({})
            counts.append({})

    placed = 0
    cyc = 0
    carry = []                    # 本拍就绪但没排上的，下一拍继续
    while placed < n:
        pool = list(carry)
        carry = []
        while waiting and waiting[0][0] <= cyc:
            _, negh, op = heappop(waiting)
            pool.append((negh, op))
        if not pool:
            if waiting:           # 没就绪算子：跳到下一个 earliest
                cyc = waiting[0][0]
                continue
            break
        ensure(cyc)
        pool.sort()               # (-height, op)：高度大者先
        used = counts[cyc]
        for negh, op in pool:
            eng = ops[op][0]
            if used.get(eng, 0) < SLOT_LIMITS[eng]:
                bundles[cyc].setdefault(eng, []).append(ops[op][1])
                used[eng] = used.get(eng, 0) + 1
                sched[op] = cyc
                placed += 1
                for w in succ[op]:
                    remaining[w] -= 1
                    if remaining[w] == 0:
                        ea = 0
                        for (u, g) in preds[w]:
                            if sched[u] + g > ea:
                                ea = sched[u] + g
                        heappush(waiting, (ea, -height[w], w))
            else:
                carry.append((negh, op))
        cyc += 1

    return [b for b in bundles if b]


# 注：曾有 flatten()/reschedule() 把「已调度的 bundle 列表」摊平后重排（用于验证打包器）。
# 已删除——它对含「同 bundle WAR（读旧值）」的算子流不安全：flatten 按引擎次序线性化会打乱
# 同 bundle 内读/写的先后，重排时把 WAR 误判成 RAW（读新值），改变语义。打包器的正确性改由
# logix/bridge.py 对 build_kernel 的真实输出做逐字节 golden 验证（对任意随机输入）。
