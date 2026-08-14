CFLAGS ?= -O2 -Wall -Wextra
PREFIX ?= $(HOME)/.local

link2c: link2c.c
	$(CC) $(CFLAGS) -o $@ $<

install: link2c
	install -Dm755 link2c $(PREFIX)/bin/link2c

clean:
	rm -f link2c
.PHONY: install clean
