"""
Python <-> C++ 桥 + golden 对拍（DESIGN §3.4 / PLAN P1.2）。

职责：
  1. 把 KernelBuilder.instrs + 初始 mem 导出成 vliwsim_run 的行式交换格式。
  2. 跑 C++ 模型 vliwsim_run，取回 BILLED + 最终 MEM。
  3. 与冻结的 frozen_problem.Machine（提交模式：debug/pause 关）逐字节/逐拍对拍。

不碰 tests/：只 import frozen_problem（只读），不修改。
用法：
  python bridge.py            # 对基线 kernel 做 golden 对拍
"""
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)  # takehome 仓根
TESTS = os.path.join(ROOT, "tests")
sys.path.insert(0, ROOT)
sys.path.insert(0, TESTS)

# 提交口径的机器/参考都来自 frozen_problem（与 submission_tests 一致）
from frozen_problem import (  # noqa: E402
    Machine,
    build_mem_image,
    reference_kernel2,
    Tree,
    Input,
    N_CORES,
    SCRATCH_SIZE,
)
from perf_takehome import KernelBuilder  # noqa: E402

VLIWSIM_RUN = os.path.join(HERE, "vliwsim_run")


def export_program(instrs, mem, scratch_size=SCRATCH_SIZE):
    """把 instrs + mem 序列化成交换格式字符串（丢弃 debug slot，保留空 bundle 以对齐计费/pc）。"""
    lines = []
    lines.append(f"SCRATCH {scratch_size}")
    lines.append(f"MEM {len(mem)}")
    # mem 值逐行 32 个
    row = []
    for i, v in enumerate(mem):
        row.append(str(v & 0xFFFFFFFF))
        if len(row) == 32:
            lines.append(" ".join(row))
            row = []
    if row:
        lines.append(" ".join(row))
    lines.append(f"PROG {len(instrs)}")
    for bundle in instrs:
        slots = []
        for engine, engine_slots in bundle.items():
            if engine == "debug":
                continue  # 提交模式 debug 不计费、不影响结果 → 丢弃
            for slot in engine_slots:
                op = slot[0]
                args = slot[1:]
                slots.append((engine, op, args))
        lines.append(f"BUNDLE {len(slots)}")
        for engine, op, args in slots:
            arg_str = " ".join(str(int(x)) for x in args)
            lines.append(f"{engine} {op} {len(args)} {arg_str}".rstrip())
    return "\n".join(lines) + "\n"


def run_model_full(instrs, mem, scratch_size=SCRATCH_SIZE, trace_prefix=None):
    """跑 C++ 模型，返回 (billed, occ_dict, final_mem)。trace_prefix 非空则开 trace 落盘。"""
    if not os.path.exists(VLIWSIM_RUN):
        raise RuntimeError(f"未找到 {VLIWSIM_RUN}，先 `make`。")
    text = export_program(instrs, mem, scratch_size)
    with tempfile.NamedTemporaryFile("w", suffix=".vsim", delete=False) as f:
        f.write(text)
        path = f.name
    cmd = [VLIWSIM_RUN, path]
    if trace_prefix:
        cmd += ["--trace", trace_prefix]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, check=True).stdout
    finally:
        os.unlink(path)
    return parse_output(out)


def run_model(instrs, mem, scratch_size=SCRATCH_SIZE):
    """跑 C++ 模型，返回 (billed, final_mem)。"""
    billed, _occ, final_mem = run_model_full(instrs, mem, scratch_size)
    return billed, final_mem


def parse_output(out):
    toks = out.split()
    i = 0
    billed = None
    occ = {}
    final_mem = None
    while i < len(toks):
        if toks[i] == "BILLED":
            billed = int(toks[i + 1])
            i += 2
        elif toks[i] == "OCC":
            occ[toks[i + 1]] = int(toks[i + 2])
            i += 3
        elif toks[i] == "MEM":
            n = int(toks[i + 1])
            i += 2
            final_mem = [int(x) for x in toks[i : i + n]]
            i += n
        else:
            i += 1
    return billed, occ, final_mem


def run_python_machine(instrs, mem, debug_info):
    """跑冻结机器（提交模式），返回 (cycle, final_mem)。mem 会被内部 copy，不改入参。"""
    machine = Machine(mem, instrs, debug_info, n_cores=N_CORES)
    machine.enable_pause = False
    machine.enable_debug = False
    machine.run()
    return machine.cycle, list(machine.mem)


def golden_check(forest_height=10, rounds=16, batch_size=256, verbose=True):
    import random

    random.seed(123)
    forest = Tree.generate(forest_height)
    inp = Input.generate(forest, batch_size, rounds)
    mem = build_mem_image(forest, inp)

    kb = KernelBuilder()
    kb.build_kernel(forest.height, len(forest.values), len(inp.indices), rounds)

    # Python 参照机器（提交模式）
    py_billed, py_mem = run_python_machine(kb.instrs, mem, kb.debug_info())

    # C++ 模型
    cpp_billed, cpp_mem = run_model(kb.instrs, mem)

    # 参考正确性（reference_kernel2 会就地改 mem 的一份拷贝）
    ref_mem = list(mem)
    for _ in reference_kernel2(ref_mem):
        pass
    inp_values_p = ref_mem[6]
    nvals = len(inp.values)

    ok_billed = cpp_billed == py_billed
    ok_mem = cpp_mem == py_mem
    py_slice = py_mem[inp_values_p : inp_values_p + nvals]
    ref_slice = ref_mem[inp_values_p : inp_values_p + nvals]
    ok_correct = py_slice == ref_slice

    # 逐字节 mism 统计
    mism = sum(1 for x, y in zip(cpp_mem, py_mem) if x != y)

    if verbose:
        print(f"[golden] forest_height={forest_height} rounds={rounds} batch={batch_size}")
        print(f"  python cycle = {py_billed}")
        print(f"  cpp    billed= {cpp_billed}   {'OK' if ok_billed else 'MISMATCH'}")
        print(f"  mem 逐字节 mism = {mism}/{len(py_mem)}   {'OK' if ok_mem else 'MISMATCH'}")
        print(f"  参考正确性(inp_values) {'OK' if ok_correct else 'MISMATCH'}")
        if not ok_mem:
            # 打印前几个不一致点
            diffs = [(i, cpp_mem[i], py_mem[i]) for i in range(len(py_mem)) if cpp_mem[i] != py_mem[i]]
            print(f"  首个不一致(最多10): {diffs[:10]}")

    return ok_billed and ok_mem and ok_correct


if __name__ == "__main__":
    ok = golden_check()
    print("GOLDEN", "PASS" if ok else "FAIL")
    sys.exit(0 if ok else 1)
