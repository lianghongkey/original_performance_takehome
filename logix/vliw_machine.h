// VLIW/SIMD 机器的 logix 忠实性能模型（takehome problem.py::Machine 的 C++ 重建）。
//
// 设计见 ./DESIGN.md。要点：
//  - 一个 VliwCore : ClkModule，一条 bundle 一个 Cycle()。
//  - 周期末统一提交：一条 bundle 内所有 slot 读旧值、写暂存，全部执行完再刷回（§1.3）。
//  - 计费口径：含非 debug slot 的 bundle 计 1 拍（billed_）；提交模式 debug/pause 关掉。
//  - 逐引擎 slot 占用出 Trace（roofline 数据）。
//  - 语义一律以 problem.py 为准，靠 golden 对拍守住。
#ifndef VLIWSIM_VLIW_MACHINE_H
#define VLIWSIM_VLIW_MACHINE_H

#include <cstdint>
#include <istream>
#include <string>
#include <vector>

#include "base/clock.h"
#include "base/module.h"

namespace vsim {

using logix::Clock;
using logix::ClkModule;
using logix::ClockPtr;

static constexpr int VLEN = 8;

enum Engine { ALU = 0, VALU = 1, LOAD = 2, STORE = 3, FLOW = 4, N_ENGINE = 5 };

// 每拍每引擎 slot 上限（= 每拍吞吐）。对齐 problem.py::SLOT_LIMITS（debug 不建模）。
static constexpr int kSlotLimit[N_ENGINE] = {12, 6, 2, 2, 1};

inline const char* EngineName(int e) {
  switch (e) {
    case ALU: return "alu";
    case VALU: return "valu";
    case LOAD: return "load";
    case STORE: return "store";
    case FLOW: return "flow";
    default: return "?";
  }
}

inline int EngineFromName(const std::string& s) {
  if (s == "alu") return ALU;
  if (s == "valu") return VALU;
  if (s == "load") return LOAD;
  if (s == "store") return STORE;
  if (s == "flow") return FLOW;
  return -1;  // debug / 未知：调用方应已在导出时剔除
}

struct Slot {
  int engine;
  std::string op;
  std::vector<int64_t> args;
};

using Bundle = std::vector<Slot>;

struct Program {
  uint32_t scratch_size = 0;
  std::vector<uint32_t> mem;
  std::vector<Bundle> bundles;
};

// 按 DESIGN §3.4 的行式交换格式加载：
//   SCRATCH <size>
//   MEM <n> \n <n 个 uint32>
//   PROG <B>
//   （每 bundle:）BUNDLE <s> \n <s 行: engine op nargs a1 a2 ...>
Program LoadProgram(std::istream& in);

class VliwCore : public ClkModule {
 public:
  enum State { RUNNING, PAUSED, STOPPED };

  VliwCore(ClockPtr c, Program prog, const std::string& name = "vliw",
           bool enable_pause = false, bool trace = false)
      : ClkModule(c),
        clk_(c),
        prog_(std::move(prog)),
        enable_pause_(enable_pause),
        trace_(trace) {
    RegisterId(name, 0);
    scratch_.assign(prog_.scratch_size, 0);
    mem_ = prog_.mem;  // 拷一份，跑程序改这份
  }

  void Cycle() override {
    DelayCycle(1);
    if (state_ != RUNNING) { clk_->Stop(); return; }
    if (pc_ >= prog_.bundles.size()) { state_ = STOPPED; clk_->Stop(); return; }

    const Bundle& b = prog_.bundles[pc_++];
    pend_s_.clear();
    pend_m_.clear();
    int occ[N_ENGINE] = {0, 0, 0, 0, 0};

    for (const Slot& s : b) {
      Exec(s);
      if (s.engine >= 0 && s.engine < N_ENGINE) ++occ[s.engine];
    }
    // 周期末统一提交：先 scratch 再 mem，同址后写覆盖先写（对齐 dict 语义）。
    for (auto& kv : pend_s_) scratch_[kv.first] = kv.second;
    for (auto& kv : pend_m_) mem_[kv.first] = kv.second;

    if (!b.empty()) {  // 含非 debug slot（debug 已在导出时剔除）→ 计 1 拍
      ++billed_;
      for (int e = 0; e < N_ENGINE; ++e) occ_total_[e] += (uint64_t)occ[e];
      if (trace_) {
        for (int e = 0; e < N_ENGINE; ++e)
          Trace(std::string(EngineName(e)) + "_busy", (uint64_t)occ[e]);
        Trace("billed", billed_);
      }
    }
  }

  uint64_t Billed() const { return billed_; }
  // 该引擎在整个程序里被执行的 slot 总数（roofline 数据；本机无 stall，占用即静态工作量之和）。
  uint64_t OccTotal(int e) const { return occ_total_[e]; }
  const std::vector<uint32_t>& Mem() const { return mem_; }
  const std::vector<uint32_t>& Scratch() const { return scratch_; }
  bool Stopped() const { return state_ == STOPPED; }

 private:
  static constexpr uint64_t M32 = 0xFFFFFFFFull;

  static uint64_t Alu(const std::string& op, uint64_t a, uint64_t b) {
    uint64_t r = 0;
    if (op == "+") r = a + b;
    else if (op == "-") r = a - b;
    else if (op == "*") r = a * b;
    else if (op == "//") r = a / b;
    else if (op == "cdiv") r = (a + b - 1) / b;
    else if (op == "^") r = a ^ b;
    else if (op == "&") r = a & b;
    else if (op == "|") r = a | b;
    else if (op == "<<") r = (b >= 64) ? 0 : (a << b);
    else if (op == ">>") r = (b >= 64) ? 0 : (a >> b);
    else if (op == "%") r = a % b;
    else if (op == "<") r = (a < b) ? 1 : 0;
    else if (op == "==") r = (a == b) ? 1 : 0;
    else LOGCHECK(false, "unknown alu op");
    return r & M32;
  }

  uint32_t S(int64_t addr) const { return scratch_[(size_t)addr]; }
  void WS(int64_t addr, uint64_t v) { pend_s_.emplace_back((uint32_t)addr, (uint32_t)(v & M32)); }
  void WM(int64_t addr, uint64_t v) { pend_m_.emplace_back((uint32_t)addr, (uint32_t)(v & M32)); }

  void Exec(const Slot& s) {
    const auto& a = s.args;
    const std::string& op = s.op;
    switch (s.engine) {
      case ALU:
        WS(a[0], Alu(op, S(a[1]), S(a[2])));
        break;
      case VALU:
        if (op == "vbroadcast") {
          for (int i = 0; i < VLEN; ++i) WS(a[0] + i, S(a[1]));
        } else if (op == "multiply_add") {
          for (int i = 0; i < VLEN; ++i)
            WS(a[0] + i, (S(a[1] + i) * (uint64_t)S(a[2] + i) + S(a[3] + i)) & M32);
        } else {  // 逐元素：op 是普通 alu 运算
          for (int i = 0; i < VLEN; ++i) WS(a[0] + i, Alu(op, S(a[1] + i), S(a[2] + i)));
        }
        break;
      case LOAD:
        if (op == "load") {
          WM_load(a[0], S(a[1]));
        } else if (op == "load_offset") {
          int64_t off = a[2];
          WM_load(a[0] + off, S(a[1] + off));
        } else if (op == "vload") {
          uint32_t base = S(a[1]);
          for (int i = 0; i < VLEN; ++i) WM_load(a[0] + i, base + i);
        } else if (op == "const") {
          WS(a[0], (uint64_t)a[1]);
        } else {
          LOGCHECK(false, "unknown load op");
        }
        break;
      case STORE:
        if (op == "store") {
          WM(S(a[0]), S(a[1]));
        } else if (op == "vstore") {
          uint32_t base = S(a[0]);
          for (int i = 0; i < VLEN; ++i) WM(base + i, S(a[1] + i));
        } else {
          LOGCHECK(false, "unknown store op");
        }
        break;
      case FLOW:
        ExecFlow(s);
        break;
      default:
        LOGCHECK(false, "unknown engine");
    }
  }

  // load 读 mem（旧值）写 scratch（暂存）。
  void WM_load(int64_t dest, uint32_t mem_addr) { WS(dest, mem_[mem_addr]); }

  void ExecFlow(const Slot& s) {
    const auto& a = s.args;
    const std::string& op = s.op;
    if (op == "select") {
      WS(a[0], S(a[1]) != 0 ? S(a[2]) : S(a[3]));
    } else if (op == "add_imm") {
      WS(a[0], (S(a[1]) + (uint64_t)a[2]) & M32);
    } else if (op == "vselect") {
      for (int i = 0; i < VLEN; ++i)
        WS(a[0] + i, S(a[1] + i) != 0 ? S(a[2] + i) : S(a[3] + i));
    } else if (op == "halt") {
      state_ = STOPPED;
    } else if (op == "pause") {
      if (enable_pause_) state_ = PAUSED;
    } else if (op == "trace_write") {
      trace_buf_.push_back(S(a[0]));
    } else if (op == "cond_jump") {
      if (S(a[0]) != 0) pc_ = (size_t)a[1];
    } else if (op == "cond_jump_rel") {
      if (S(a[0]) != 0) pc_ = pc_ + (int64_t)a[1];
    } else if (op == "jump") {
      pc_ = (size_t)a[0];
    } else if (op == "jump_indirect") {
      pc_ = S(a[0]);
    } else if (op == "coreid") {
      WS(a[0], 0);  // 单核 id=0
    } else {
      LOGCHECK(false, "unknown flow op");
    }
  }

  ClockPtr clk_;
  Program prog_;
  bool enable_pause_;
  bool trace_;

  std::vector<uint32_t> scratch_;
  std::vector<uint32_t> mem_;
  std::vector<std::pair<uint32_t, uint32_t>> pend_s_;
  std::vector<std::pair<uint32_t, uint32_t>> pend_m_;
  std::vector<uint32_t> trace_buf_;

  size_t pc_ = 0;
  uint64_t billed_ = 0;
  uint64_t occ_total_[N_ENGINE] = {0, 0, 0, 0, 0};
  State state_ = RUNNING;
};

}  // namespace vsim

#endif  // VLIWSIM_VLIW_MACHINE_H
