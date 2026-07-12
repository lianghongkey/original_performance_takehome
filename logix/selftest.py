"""
交叉验证：手工小 kernel 逐一对拍 C++ 模型 vs frozen_problem.Machine（PLAN P1.2）。
覆盖基线没触及的语义：VLIW 多 slot 打包、同 bundle RAW 危险、SIMD、mod-2^32 回卷、
flow select/跳转与计费对齐。

用法： python selftest.py
"""
import sys

from bridge import run_model, run_python_machine, ROOT, TESTS  # noqa: F401
from frozen_problem import DebugInfo  # noqa: E402


def check(name, program, mem):
    di = DebugInfo(scratch_map={})
    py_c, py_mem = run_python_machine(program, list(mem), di)
    cpp_c, cpp_mem = run_model(program, list(mem))
    mism = sum(1 for a, b in zip(py_mem, cpp_mem) if a != b)
    ok = (py_c == cpp_c) and (py_mem == cpp_mem)
    tag = "OK  " if ok else "FAIL"
    print(f"{tag} {name}: py_cyc={py_c} cpp_billed={cpp_c} mem_mism={mism}")
    if not ok:
        diffs = [(i, py_mem[i], cpp_mem[i]) for i in range(len(py_mem)) if py_mem[i] != cpp_mem[i]]
        print(f"      首个不一致(最多8): {diffs[:8]}")
    return ok


def main():
    results = []

    # A: 多 slot 打包 + 同 bundle RAW 危险（消费者读旧值）
    progA = [
        {"load": [("const", 0, 5), ("const", 1, 7)]},
        {"alu": [("+", 2, 0, 1), ("+", 3, 2, 0)]},  # s2=12; s3 读旧 s2(0)+s0(5)=5
        {"load": [("const", 4, 10), ("const", 5, 11)]},
        {"store": [("store", 4, 2), ("store", 5, 3)]},  # mem[10]=12, mem[11]=5
    ]
    results.append(check("A_pack+RAW_hazard", progA, [0] * 16))

    # B: multiply_add / vbroadcast / 逐元素 valu / vstore
    progB = [
        {"load": [("const", 0, 3), ("const", 1, 4)]},
        {"load": [("const", 2, 5)]},
        {"valu": [("vbroadcast", 8, 0)]},   # s8..15 = 3
        {"valu": [("vbroadcast", 16, 1)]},  # s16..23 = 4
        {"valu": [("vbroadcast", 24, 2)]},  # s24..31 = 5
        {"valu": [("multiply_add", 32, 8, 16, 24)]},  # 3*4+5 = 17
        {"valu": [("+", 40, 8, 16)]},        # 3+4 = 7
        {"load": [("const", 3, 48)]},
        {"store": [("vstore", 3, 32)]},      # mem[48..55]=17
        {"load": [("const", 4, 56)]},
        {"store": [("vstore", 4, 40)]},      # mem[56..63]=7
    ]
    results.append(check("B_valu_mac+broadcast+vstore", progB, [0] * 64))

    # C: vload + 向量左移
    memC = [i + 1 for i in range(8)] + [0] * 24  # mem[0..7]=1..8
    progC = [
        {"load": [("const", 0, 0)]},
        {"load": [("vload", 8, 0)]},          # s8..15 = 1..8
        {"load": [("const", 1, 3)]},
        {"valu": [("vbroadcast", 16, 1)]},    # s16..23 = 3
        {"valu": [("<<", 24, 8, 16)]},        # (1..8)<<3 = 8..64
        {"load": [("const", 2, 16)]},
        {"store": [("vstore", 2, 24)]},       # mem[16..23]
    ]
    results.append(check("C_vload+shift", progC, memC))

    # D: mod-2^32 回卷（乘法溢出 + 减法下溢）
    progD = [
        {"load": [("const", 0, 0x10000)]},    # 65536
        {"alu": [("*", 1, 0, 0)]},            # 2^32 → 0
        {"load": [("const", 2, 30)]},
        {"store": [("store", 2, 1)]},         # mem[30]=0
        {"load": [("const", 5, 1)]},
        {"alu": [("-", 6, 4, 5)]},            # s4(0) - 1 = 2^32-1
        {"load": [("const", 7, 31)]},
        {"store": [("store", 7, 6)]},         # mem[31]=4294967295
    ]
    results.append(check("D_mod2^32_wrap", progD, [0] * 33))

    # E: flow select / add_imm
    progE = [
        {"load": [("const", 0, 0), ("const", 1, 99)]},
        {"flow": [("add_imm", 2, 1, 1)]},     # 99+1=100
        {"load": [("const", 3, 5), ("const", 4, 6)]},
        {"flow": [("select", 5, 0, 3, 4)]},   # cond s0=0 → b=6
        {"load": [("const", 6, 7), ("const", 7, 8)]},
        {"flow": [("select", 8, 7, 6, 7)]},   # cond s7=8 → a=7
        {"load": [("const", 9, 20)]},
        {"store": [("store", 9, 2)]},         # mem[20]=100
        {"load": [("const", 10, 21)]},
        {"store": [("store", 10, 5)]},        # mem[21]=6
        {"load": [("const", 11, 22)]},
        {"store": [("store", 11, 8)]},        # mem[22]=7
    ]
    results.append(check("E_flow_select+add_imm", progE, [0] * 24))

    # F: cond_jump 跳过 bundle + 计费对齐
    progF = [
        {"load": [("const", 0, 1)]},          # 0: s0=1
        {"flow": [("cond_jump", 0, 4)]},      # 1: s0!=0 → pc=4，跳过 2,3
        {"load": [("const", 1, 111)]},        # 2: skipped
        {"load": [("const", 2, 222)]},        # 3: skipped
        {"load": [("const", 3, 40)]},         # 4: s3=40
        {"store": [("store", 3, 0)]},         # 5: mem[40]=1
    ]
    # 执行 bundle 0,1,4,5 = 4 拍；mem[40]=1，且 mem 里不应出现 111/222
    results.append(check("F_cond_jump+billed", progF, [0] * 41))

    # G: 混合多引擎打包（alu+load+store+flow 同一 bundle，各不超上限）
    memG = [100, 200] + [0] * 30
    progG = [
        {"load": [("const", 0, 0), ("const", 1, 1)]},   # s0=0(地址), s1=1(地址)
        {"load": [("load", 2, 0), ("load", 3, 1)]},      # s2=mem[0]=100, s3=mem[1]=200
        {"load": [("const", 4, 10), ("const", 5, 11)]},
        # 同 bundle：alu 算 s6=s2+s3(=300)；load 读 mem[0]；store 写 mem[10]=s2(旧,100)；flow add_imm
        {
            "alu": [("+", 6, 2, 3)],
            "load": [("const", 7, 12)],
            "store": [("store", 4, 2)],       # mem[10]=100
            "flow": [("add_imm", 8, 3, 5)],   # s8=200+5=205
        },
        {"store": [("store", 5, 6)]},         # mem[11]=300
        {"load": [("const", 9, 13)]},
        {"store": [("store", 7, 8)]},         # mem[12]=205
    ]
    results.append(check("G_mixed_engine_pack", progG, memG))

    print()
    npass = sum(results)
    print(f"SELFTEST {npass}/{len(results)} PASS")
    return npass == len(results)


if __name__ == "__main__":
    sys.exit(0 if main() else 1)
