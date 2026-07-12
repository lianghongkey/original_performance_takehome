// 驱动：读交换格式 → 在 logix 上跑 VliwCore → 输出 BILLED + 最终 MEM。
//   用法: vliwsim_run <input|-> [--trace <prefix>] [--pause]
// 输出(stdout):
//   BILLED <n>
//   MEM <n>
//   v0 v1 ... v(n-1)
#include <cstdio>
#include <fstream>
#include <iostream>
#include <sstream>
#include <string>

#include "base/runtime.h"
#include "base/signal_tracer.h"
#include "vliw_machine.h"

using namespace vsim;

Program vsim::LoadProgram(std::istream& in) {
  Program p;
  std::string tok;
  while (in >> tok) {
    if (tok == "SCRATCH") {
      in >> p.scratch_size;
    } else if (tok == "MEM") {
      size_t n;
      in >> n;
      p.mem.resize(n);
      for (size_t i = 0; i < n; ++i) {
        uint64_t v;
        in >> v;
        p.mem[i] = (uint32_t)v;
      }
    } else if (tok == "PROG") {
      size_t B;
      in >> B;
      p.bundles.reserve(B);
      for (size_t bi = 0; bi < B; ++bi) {
        std::string bt;
        size_t s;
        in >> bt >> s;  // "BUNDLE" <s>
        Bundle bundle;
        bundle.reserve(s);
        for (size_t si = 0; si < s; ++si) {
          std::string eng, op;
          int na;
          in >> eng >> op >> na;
          Slot slot;
          slot.engine = EngineFromName(eng);
          slot.op = op;
          slot.args.resize(na);
          for (int k = 0; k < na; ++k) in >> slot.args[k];
          bundle.push_back(std::move(slot));
        }
        p.bundles.push_back(std::move(bundle));
      }
    }
  }
  return p;
}

int main(int argc, char** argv) {
  std::string input;
  std::string trace_prefix;
  bool enable_pause = false;
  bool trace = false;
  for (int i = 1; i < argc; ++i) {
    std::string a = argv[i];
    if (a == "--trace" && i + 1 < argc) {
      trace = true;
      trace_prefix = argv[++i];
    } else if (a == "--pause") {
      enable_pause = true;
    } else {
      input = a;
    }
  }
  if (input.empty()) {
    std::cerr << "usage: vliwsim_run <input|-> [--trace <prefix>] [--pause]\n";
    return 2;
  }

  Program prog;
  if (input == "-") {
    prog = LoadProgram(std::cin);
  } else {
    std::ifstream f(input);
    if (!f) {
      std::cerr << "cannot open " << input << "\n";
      return 2;
    }
    prog = LoadProgram(f);
  }

  if (!trace) logix::SetTraceDisabled(true);  // 零开销跑

  // 结果先拷到局部：core（及其 tracer）必须先出作用域析构、把 trace 尾段刷进 Recorder，
  // 之后才能 FlushRecorder()（Finalize）—— 否则 tracer 析构在 Finalize 后 AppendSegment 触发断言。
  uint64_t billed = 0;
  uint64_t occ[N_ENGINE] = {0, 0, 0, 0, 0};
  std::vector<uint32_t> mem;
  {
    logix::RT::Reset();
    ClockPtr clk = logix::MakeClock(0, 10);
    VliwCore core(clk, std::move(prog), "vliw", enable_pause, trace);
    clk->Continue();  // 无界，靠 core 的 clk->Stop() 收尾
    logix::RT::JoinAll();
    billed = core.Billed();
    for (int e = 0; e < N_ENGINE; ++e) occ[e] = core.OccTotal(e);
    mem = core.Mem();
  }  // core + tracer 析构：trace 尾段刷入 Recorder

  // 输出结果
  std::ostringstream out;
  out << "BILLED " << billed << "\n";
  for (int e = 0; e < N_ENGINE; ++e)  // 逐引擎 slot 总量（roofline 数据）
    out << "OCC " << EngineName(e) << " " << occ[e] << "\n";
  out << "MEM " << mem.size() << "\n";
  for (size_t i = 0; i < mem.size(); ++i) {
    out << mem[i];
    out << ((i + 1) % 32 == 0 ? '\n' : ' ');
  }
  out << "\n";
  std::cout << out.str();

  if (trace) {
    logix::RT::FlushRecorder();  // Finalize：此时 trace 文件才完整（tracer 已析构刷完尾段）
    const std::string src = logix::RT::GetRecorder().PathPrefix();  // 默认 "Recorder"
    std::cerr << "[trace] 写到 " << src << ".trace";
    if (!trace_prefix.empty() && trace_prefix != src) {
      std::rename((src + ".trace").c_str(), (trace_prefix + ".trace").c_str());
      std::rename((src + ".strings.json").c_str(), (trace_prefix + ".strings.json").c_str());
      std::cerr << " → 重命名为 " << trace_prefix << ".trace";
    }
    std::cerr << "\n";
  }
  return 0;
}
