# macOS / Linux:  make
# Windows (MinGW-w64, e.g. from MSYS2):  mingw32-make WINDOWS=1
CC ?= cc
CFLAGS ?= -O2 -Wall -Wextra -std=c11 -D_POSIX_C_SOURCE=200809L
SRC = etf_arb.c rit.c http.c json.c
LIBS = -lm

ifeq ($(WINDOWS),1)
  LIBS += -lws2_32
  OUT = etf_arb.exe
else
  OUT = etf_arb
endif

$(OUT): $(SRC) rit.h http.h json.h
	$(CC) $(CFLAGS) -o $@ $(SRC) $(LIBS)

test_json: test_json.c json.c json.h
	$(CC) $(CFLAGS) -o $@ test_json.c json.c $(LIBS)
	./test_json

clean:
	rm -f etf_arb etf_arb.exe test_json
