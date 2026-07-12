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

- **成绩：147734 → 1321 拍（111.8×），通过全部 9 个提交测试**（含 `<1363`，超过 Readme 已知最好），
  全程对 `frozen_problem.Machine` **逐字节 mism=0**、`tests/` 未改。
- 模型驱动轨迹：2107→…→1481（ALU 分流）→1377（pre-offset）→1357（L0 特判）→**1321（删死 wrap）**。
  每步由 `roofline.py` 指出新瓶颈、`bridge.py` 守 golden、`--trace` 定位空转。
- 现为 **compute-bound**（valu ~1195）——hash 逐元素 4096 次是根本工作量，靠 valu+alu 双引擎分流逼近下界。
- 优化后的 kernel 在 `../perf_takehome.py::KernelBuilder.build_kernel`，打包器在 `../vliw_sched.py`。

## 不变量

- 不改 `tests/`；成绩以 `python tests/submission_tests.py` 为准。
- 模型语义分歧一律以 `problem.py` 为准，靠 golden 守。
