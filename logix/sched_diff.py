"""调度诊断：重放 vliw_sched.schedule() 的贪心逻辑，记录每条算子的 (bundle, 约束原因)，
并对比两组 env 配置下的逐算子 placement 差——定位「某个改动为什么让打包变差」。

用法：
  python sched_diff.py NVMERGE=0 -- NVMERGE=1      # 对比两组旋钮（-- 分隔）
  python sched_diff.py -- EARLYW=8                  # 左边为默认配置

输出：首个分叉算子、延后最大的算子、按 (engine, 约束原因) 聚类的延后统计。
约束原因里 RAW@a/WAR@a/WAW@a 指对 scratch 地址 a 的依赖，slots 指该引擎槽位满。
典型用途：发现「共享临时毒化 run-ahead」（后发组的早轮 WAR 在先发组晚轮之后）这类
程序序 ≠ 执行序的打包病灶（见 RESULTS.md「run-ahead 毒化」一节）。
"""
import os
import sys
import random

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from problem import SLOT_LIMITS, Tree, Input  # noqa: E402
import vliw_sched  # noqa: E402


def placements(ops):
    """重放 schedule() 的贪心placement，返回 [(bundle_idx, 约束原因), ...]（与 ops 对齐）。"""
    bundles, counts = [], []
    last_write, last_read = {}, {}
    last_mem_write, last_mem_read = {}, {}
    barrier_at, max_used = -1, -1
    first_free = {e: 0 for e in SLOT_LIMITS}
    place = []

    def ensure(idx):
        while len(bundles) <= idx:
            bundles.append({})
            counts.append({})

    for op in ops:
        engine, slot = op[0], op[1]
        region = op[2] if len(op) > 2 else None
        reads, writes, mr, mw, barrier = vliw_sched.slot_rw(engine, slot)
        if barrier:
            idx = max(max_used + 1, barrier_at + 1)
            ensure(idx)
            counts[idx][engine] = counts[idx].get(engine, 0) + 1
            barrier_at = idx
            max_used = max(max_used, idx)
            place.append((idx, "barrier"))
            continue
        earliest, why = barrier_at + 1, "barrier"
        for r in reads:
            w = last_write.get(r, -1)
            if w + 1 > earliest:
                earliest, why = w + 1, f"RAW@{r}"
        for w in writes:
            rd = last_read.get(w, -1)
            if rd > earliest:
                earliest, why = rd, f"WAR@{w}"
            pw = last_write.get(w, -1)
            if pw + 1 > earliest:
                earliest, why = pw + 1, f"WAW@{w}"
        lmw = last_mem_write.get(region, -1)
        lmr = last_mem_read.get(region, -1)
        if mr and lmw + 1 > earliest:
            earliest, why = lmw + 1, "memRAW"
        if mw:
            if lmr > earliest:
                earliest, why = lmr, "memWAR"
            if lmw > earliest:
                earliest, why = lmw, "memWAW"
        limit = SLOT_LIMITS[engine]
        idx = earliest if earliest > first_free[engine] else first_free[engine]
        ensure(idx)
        while counts[idx].get(engine, 0) >= limit:
            idx += 1
            ensure(idx)
        if idx > earliest:
            why = "slots"
        if idx == first_free[engine] and counts[idx].get(engine, 0) + 1 >= limit:
            ff = idx + 1
            ensure(ff)
            while counts[ff].get(engine, 0) >= limit:
                ff += 1
                ensure(ff)
            first_free[engine] = ff
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
        place.append((idx, why))
    return place


def build(env):
    saved = {}
    for k, v in env.items():
        saved[k] = os.environ.get(k)
        os.environ[k] = v
    try:
        import importlib
        import perf_takehome
        importlib.reload(perf_takehome)
        random.seed(123)
        f = Tree.generate(10)
        inp = Input.generate(f, 256, 16)
        kb = perf_takehome.KernelBuilder()
        kb.build_kernel(f.height, len(f.values), len(inp.indices), 16)
        return kb._ops, len(kb.instrs)
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def main():
    args = sys.argv[1:]
    sep = args.index("--") if "--" in args else len(args)
    env_a = dict(kv.split("=", 1) for kv in args[:sep])
    env_b = dict(kv.split("=", 1) for kv in args[sep + 1:]) if sep < len(args) else {}

    ops_a, n_a = build(env_a)
    ops_b, n_b = build(env_b)
    print(f"bundles: A={n_a}  B={n_b}   (A: {env_a or '默认'} | B: {env_b or '默认'})")
    if len(ops_a) != len(ops_b):
        print(f"算子流长度不同（{len(ops_a)} vs {len(ops_b)}），逐条对齐仅对「只改地址/槽位」的变体有意义")
        return
    pa, pb = placements(ops_a), placements(ops_b)

    from collections import Counter
    lag = Counter()
    first = None
    worst = []
    for i, ((a, _wa), (b, wb)) in enumerate(zip(pa, pb)):
        if b != a and first is None:
            first = i
        if b > a:
            lag[(ops_b[i][0], wb)] += 1
            worst.append((b - a, i))
    if first is None:
        print("两版 placement 完全一致")
        return
    print(f"首个分叉 op{first}: {ops_b[first][0]} {ops_b[first][1][:2]}  {pa[first]} -> {pb[first]}")
    worst.sort(reverse=True)
    print("延后最大 top6:")
    for d, i in worst[:6]:
        print(f"  op{i} {ops_b[i][0]} {ops_b[i][1][:2]} +{d} ({pb[i][1]})")
    print("延后算子按 (engine, 约束原因) 聚类 top10:")
    for k, v in lag.most_common(10):
        print("  ", k, v)


if __name__ == "__main__":
    main()
