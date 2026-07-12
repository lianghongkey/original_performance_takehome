"""
roofline：把一个 kernel 跑过 C++ 模型，打印逐引擎 slot 总量 + 每引擎拍数下界 + 瓶颈（PLAN P1.3）。

对这台机器（无 stall/latency，一条 bundle 恰 1 拍），某引擎的拍数下界 = ceil(该引擎 slot 总数 / slot 上限)，
roofline 下界 = 所有引擎下界的最大值。实测 billed 与该下界的差 = 打包/软流水没做满的空转。

用法：
  python roofline.py                 # 基线 kernel
  python roofline.py --trace vliw    # 同时落 vliw.trace，供 insight/trace_stats.py 看逐拍波形
"""
import math
import sys

from bridge import (  # noqa: E402
    run_model_full,
    Tree,
    Input,
    build_mem_image,
    KernelBuilder,
    SCRATCH_SIZE,
)

# 与 problem.py::SLOT_LIMITS 对齐（debug 不计费）
LIMIT = {"alu": 12, "valu": 6, "load": 2, "store": 2, "flow": 1}
ORDER = ["alu", "valu", "load", "store", "flow"]


def roofline_table(billed, occ):
    print(f"  billed(实测拍数) = {billed}")
    print(f"  {'engine':6} {'slots':>10} {'limit':>6} {'下界=ceil(slots/limit)':>22} {'占billed':>10}")
    lbs = {}
    for e in ORDER:
        slots = occ.get(e, 0)
        lb = math.ceil(slots / LIMIT[e]) if slots else 0
        lbs[e] = lb
        frac = (slots / (LIMIT[e] * billed) * 100) if billed else 0.0
        print(f"  {e:6} {slots:>10} {LIMIT[e]:>6} {lb:>22} {frac:>9.1f}%")
    bottleneck = max(ORDER, key=lambda e: lbs[e])
    roof = lbs[bottleneck]
    print(f"  → roofline 下界 = {roof} 拍（瓶颈引擎: {bottleneck}）")
    if billed:
        print(f"  → 实测/下界 = {billed}/{roof} = {billed / roof:.2f}×（越接近 1 越满）")
    return roof, bottleneck


def main():
    trace_prefix = None
    if "--trace" in sys.argv:
        i = sys.argv.index("--trace")
        trace_prefix = sys.argv[i + 1] if i + 1 < len(sys.argv) else "vliw"

    forest_height, rounds, batch_size = 10, 16, 256
    import random

    random.seed(123)
    forest = Tree.generate(forest_height)
    inp = Input.generate(forest, batch_size, rounds)
    mem = build_mem_image(forest, inp)
    kb = KernelBuilder()
    kb.build_kernel(forest.height, len(forest.values), len(inp.indices), rounds)

    billed, occ, _mem = run_model_full(
        kb.instrs, mem, SCRATCH_SIZE, trace_prefix=trace_prefix
    )
    print(f"[roofline] forest_height={forest_height} rounds={rounds} batch={batch_size}")
    roofline_table(billed, occ)
    if trace_prefix:
        print(f"\n逐拍波形已落 {trace_prefix}.trace，可用："
              f"\n  python {sys.path and ''}<logix>/docs/tools/trace_stats.py {trace_prefix}.trace "
              f"alu_busy valu_busy load_busy store_busy flow_busy")


if __name__ == "__main__":
    main()
