# 自包含构建（DESIGN §3.5 方案 B）：链接预编的 logix 静态库。
# 前提：logix 已在 $(LOGIX)/build 下 cmake 构建过（liblogix_base_static.a 等已存在）。
# 覆盖 logix 路径：make LOGIX=/path/to/logix

LOGIX ?= $(abspath $(CURDIR)/../../logix)
LBUILD ?= $(LOGIX)/build

CXX ?= g++
CXXFLAGS ?= -std=c++17 -O2 -g -Wall -Wno-unused-variable -Wno-unused-parameter -Wno-sign-compare
INCLUDES = -I. -I$(LOGIX)/src -I$(LOGIX)/src/base -I$(LOGIX)/src/isa \
           -I$(LOGIX)/include -I$(LOGIX)/3rdparties/spdlog-1.11.0/include \
           -I$(LOGIX)/3rdparties/mio

LIBS = -Wl,--start-group \
         $(LBUILD)/src/liblogix_base_static.a \
         $(LBUILD)/3rdparties/spdlog-1.11.0/libspdlogd.a \
         $(LBUILD)/3rdparties/hp-socket/libhp-socket.a \
         $(LBUILD)/3rdparties/sqlite3/libsqlite3.a \
         $(LBUILD)/_deps/fmt-build/libfmtd.a \
       -Wl,--end-group -lpthread -ldl

all: vliwsim_run

vliwsim_run: vliwsim_run.cpp vliw_machine.h
	$(CXX) $(CXXFLAGS) $(INCLUDES) vliwsim_run.cpp -o $@ $(LIBS)

clean:
	rm -f vliwsim_run

.PHONY: all clean
