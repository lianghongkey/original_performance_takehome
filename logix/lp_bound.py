"""理论下界 LP：算力全部用满时的最小拍数。

工作量（语义强制，与排布无关；数字与 inv.py 实测清单对齐）：
  - hash：512 组·轮 × (4 MAC + 7 非MAC)；nodeval xor 480（r11 已 L0FOLD 折掉 32）
  - idx：448 组·轮 = 384 非L0（& + 2A-MAC + [+c: flow 或 1 算子]）+ 64 L0（& + [sel: flow 或 1 算子]）
  - select：L1 64 组·轮（flow 或 1 MAC）；L2 64（1 cmp + 1 MAC + 2 [flow 或 MAC]）；
            L3 64（3 cmp + 1 MAC + 6 [flow 或 MAC]）
  - gather：2048 + setup load 55；L4 的 64 组·轮可转 select（每个 +15 MAC + 7 cmp、−8 load）；
            L1/L2/L3 可反向转 gather（每个 +8 load、省对应算子）
  - setup：valu ~50（广播）、alu ~120、flow ~15、load 55（已含）

引擎成本：MAC → valu 1 槽 / alu 16 槽；非MAC（xor/移位/&/cmp/加）→ valu 1 / alu 8；
flow 槽 1/拍；load 2/拍。
"""
from scipy.optimize import linprog

# 变量: [C, c4, g1, g2, g3, fa, fb, fc, fd, fe, ma, pa]
#  C  拍数
#  c4 L4 gather→select 转换数(0..64)
#  g1/g2/g3 L1/L2/L3 select→gather 反向转换数(0..64)
#  fa..fe  各 flow-可选类留在 flow 上的条数
#  ma  搬到 alu 的 MAC 算子数; pa 搬到 alu 的非MAC 算子数
IDX_C, IDX_c4, IDX_g1, IDX_g2, IDX_g3 = 0, 1, 2, 3, 4
IDX_fa, IDX_fb, IDX_fc, IDX_fd, IDX_fe = 5, 6, 7, 8, 9
IDX_ma, IDX_pa = 10, 11
N = 12

HASH_MAC, IDX_MAC = 2048, 384
HASH_NM = 512 * 7
XOR = 480
PARITY = 448
SETUP_VALU, SETUP_ALU, SETUP_FLOW, SETUP_LOAD = 50, 120, 15, 55


def row(**kw):
    r = [0.0] * N
    for k, v in kw.items():
        r[globals()["IDX_" + k]] = v
    return r

A_ub, b_ub = [], []

# ── U_mac 与 U_plain 的组成（写成对变量的线性式） ──
# U_mac  = HASH_MAC + IDX_MAC + (64−g2) + (64−g3) + 15c4
#          + (64−g1−fc) + (2(64−g2)−fd) + (6(64−g3)−fe)
# U_plain= HASH_NM + XOR + PARITY + (64−g2) + 3(64−g3) + 7c4 + (384−fa) + (64−fb)
# valu 槽 = U_mac + U_plain − ma − pa + SETUP_VALU ≤ 6C
A_ub.append(row(C=-6, c4=15 + 7, g1=-1, g2=-1 - 2 - 1, g3=-1 - 6 - 3,
                fa=-1, fb=-1, fc=-1, fd=-1, fe=-1, ma=-1, pa=-1))
b_ub.append(-(HASH_MAC + IDX_MAC + 64 + 64 + 64 + 128 + 384        # MAC 部分常数
              + HASH_NM + XOR + PARITY + 64 + 192 + 384 + 64       # plain 部分常数
              + SETUP_VALU))
# alu 槽 = 16ma + 8pa + SETUP_ALU ≤ 12C
A_ub.append(row(C=-12, ma=16, pa=8))
b_ub.append(-SETUP_ALU)
# flow 槽 = fa+fb+fc+fd+fe + SETUP_FLOW ≤ C
A_ub.append(row(C=-1, fa=1, fb=1, fc=1, fd=1, fe=1))
b_ub.append(-SETUP_FLOW)
# load = 2048 + SETUP_LOAD − 8c4 + 8(g1+g2+g3) ≤ 2C
A_ub.append(row(C=-2, c4=-8, g1=8, g2=8, g3=8))
b_ub.append(-(2048 + SETUP_LOAD))
# flow 类的容量: fc ≤ 64−g1, fd ≤ 128−2g2, fe ≤ 384−6g3
A_ub.append(row(fc=1, g1=1)); b_ub.append(64)
A_ub.append(row(fd=1, g2=2)); b_ub.append(128)
A_ub.append(row(fe=1, g3=6)); b_ub.append(384)
# ma+pa 不得超过对应总量（宽松界，防止负值套利；ma ≤ U_mac, pa ≤ U_plain 用常数上界即可）
A_ub.append(row(ma=1, c4=-15, g1=1, g2=4, g3=10, fc=1, fd=1, fe=1))
b_ub.append(HASH_MAC + IDX_MAC + 64 * 3 + 128 + 384)
A_ub.append(row(pa=1, c4=-7, g2=1, g3=3, fa=1, fb=1))
b_ub.append(HASH_NM + XOR + PARITY + 64 + 192 + 384 + 64)

bounds = [(0, None), (0, 64), (0, 64), (0, 64), (0, 64),
          (0, 384), (0, 64), (0, 64), (0, 128), (0, 384),
          (0, None), (0, None)]
c = [1.0] + [0.0] * (N - 1)

res = linprog(c, A_ub=A_ub, b_ub=b_ub, bounds=bounds, method="highs")
assert res.success, res.message
v = res.x
C = v[IDX_C]
print(f"LP 最优 C = {C:.1f} 拍")
print(f"  L4 转 select c4 = {v[IDX_c4]:.1f}   L1/L2/L3 转 gather = "
      f"{v[IDX_g1]:.1f}/{v[IDX_g2]:.1f}/{v[IDX_g3]:.1f}")
print(f"  flow 留用 fa..fe = {v[IDX_fa]:.0f}/{v[IDX_fb]:.0f}/{v[IDX_fc]:.0f}/"
      f"{v[IDX_fd]:.0f}/{v[IDX_fe]:.0f}  (Σ={sum(v[IDX_fa:IDX_fe+1]):.0f})")
print(f"  搬 alu：MAC {v[IDX_ma]:.1f} 条、非MAC {v[IDX_pa]:.1f} 条")
# 各引擎占用
mac_total = (HASH_MAC + IDX_MAC + (64 - v[IDX_g2]) + (64 - v[IDX_g3]) + 15 * v[IDX_c4]
             + (64 - v[IDX_g1] - v[IDX_fc]) + (128 - 2 * v[IDX_g2] - v[IDX_fd])
             + (384 - 6 * v[IDX_g3] - v[IDX_fe]))
plain_total = (HASH_NM + XOR + PARITY + (64 - v[IDX_g2]) + 3 * (64 - v[IDX_g3])
               + 7 * v[IDX_c4] + (384 - v[IDX_fa]) + (64 - v[IDX_fb]))
valu = mac_total + plain_total - v[IDX_ma] - v[IDX_pa] + SETUP_VALU
alu = 16 * v[IDX_ma] + 8 * v[IDX_pa] + SETUP_ALU
flow = sum(v[IDX_fa:IDX_fe + 1]) + SETUP_FLOW
load = 2048 + SETUP_LOAD - 8 * v[IDX_c4] + 8 * (v[IDX_g1] + v[IDX_g2] + v[IDX_g3])
print(f"  占用: valu {valu:.0f}/{6*C:.0f}  alu {alu:.0f}/{12*C:.0f}  "
      f"flow {flow:.0f}/{C:.0f}  load {load:.0f}/{2*C:.0f}")
for nm, used, cap in (("valu", valu, 6 * C), ("alu", alu, 12 * C),
                      ("flow", flow, C), ("load", load, 2 * C)):
    print(f"    {nm}: {100*used/cap:.1f}%")
