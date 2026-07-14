# logix 性能建模 —— takehome VLIW/SIMD 机器

用 logix 框架给 takehome 这台 VLIW/SIMD 机器建的**忠实 C++ 性能模型**，作为「提高代码生成成绩」的
度量与驱动底座。设计见 [`DESIGN.md`](DESIGN.md)，计划见 [`PLAN.md`](PLAN.md)。

## 组成

| 文件 | 作用 |
| - | - |
| `vliw_machine.h` | `VliwCore : ClkModule` —— 一条 bundle 一个 `Cycle()`，周期末统一提交，逐引擎 slot 记账 + Trace |
| `vliwsim_run.cpp` | 驱动：读交换格式 → 在 logix 上跑 → 输出 `BILLED` + 逐引擎 `OCC` + 最终 `MEM`；`--trace` 落逐拍波形 |
| `Makefile` | 自包含构建（链接预编的 `logix_base_static` 等，DESIGN §3.5 方案 B） |
| `bridge.py` | Python↔C++ 桥 + golden 对拍（对 `frozen_problem.Machine` 逐字节/逐拍；打包器正确性也靠它守） |
| `selftest.py` | 手工小 kernel 交叉验证（多 slot 打包 / 同 bundle RAW 危险 / SIMD / 回卷 / 跳转 / 计费） |
| `roofline.py` | 跑 kernel → 逐引擎 slot 总量 + 每引擎拍数下界 + 瓶颈引擎 |
| `sched_diff.py` | 调度诊断：两组旋钮下逐算子对比打包 placement + 约束原因（定位 run-ahead 毒化类病灶） |
| `lp_bound.py` | 理论下界 LP：算力全部用满时的最小拍数（工作量作常数、交换手段作变量）——解得 **1016 拍**、四引擎同时 100% |

## 用法

```bash
# 前提：logix 已在 ../../logix/build 下 cmake 构建过（liblogix_base_static.a 存在）
make                        # 编 vliwsim_run；覆盖路径 make LOGIX=/path/to/logix

python bridge.py            # 基线 golden 对拍（应 GOLDEN PASS，billed==147734，mism=0）
python selftest.py          # 交叉验证（应 7/7 PASS）
python roofline.py          # 打印基线 roofline（瓶颈引擎 + 拍数下界）
python roofline.py --trace vliw   # 另落 vliw.trace，供 insight/trace_stats.py 看逐拍波形

# 逐拍占用（gmp 方法论；注意用完整 dotted 路径避子串歧义 valu_busy⊃alu_busy）
python ../../logix/docs/tools/trace_stats.py vliw.trace \
    vliw.alu_busy vliw.valu_busy vliw.load_busy vliw.store_busy vliw.flow_busy
```

## 现状（P1 建模 + P2 优化均完成，详见 [`RESULTS.md`](RESULTS.md)）

- **成绩：147734 → 1076 拍（137.3×），通过全部 9 个提交测试**（含 `<1363`，超过 Readme 已知最好），
  全程对 `frozen_problem.Machine` **逐字节 mism=0**、`tests/` 未改。
- 模型驱动轨迹：2107→…→1481（ALU 分流）→1321（删死 wrap）→1215（消 drain）→1131（S3 MAC 折叠）
  →1125（pair-MAC 线性插值替 select）→1113（CONSTFLOW + EARLYW 填 load 前窗）
  →1099（SEL15：死区广播 + MAC 链 select 卸载 r15 gather）→1094（TAILG+TAILB 缩末组终链）
  →1089（u 并行 MAC：hash 链 9→8 级）→1078（SEL15A/SEL4A 纯 ALU select 免广播墙 + L1SEL/L0FOLD/IDXFLOW 微平衡）→**1076（pause 寄生注入）**。
  每步由 `roofline.py` 指出新瓶颈、`bridge.py` 守 golden、`--trace`/`sched_diff.py`/逐 bundle 解剖
  定位空转与打包病灶。
- 现为 **valu-bound**（valu 1047 / load 1040，billed=1.03× roofline）——已贴住打包极限；
  `lp_bound.py` 给出**算力全满的理论下界 1016 拍**（四引擎同时 100%，当前结构即 LP 最优形状），
  1076 = 1.06× 下界，剩余差距全在时间结构/头尾依赖/整数粒度（现实地板 ≈ 1045–1060）。
- 优化后的 kernel 在 `../perf_takehome.py::KernelBuilder.build_kernel`，打包器在 `../vliw_sched.py`。

## 不变量

- 不改 `tests/`；成绩以 `python tests/submission_tests.py` 为准。
- 模型语义分歧一律以 `problem.py` 为准，靠 golden 守。
