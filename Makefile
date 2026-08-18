CFLAGS ?= -O2 -Wall -Wextra
PREFIX ?= $(HOME)/.local

link2c: link2c.c
	$(CC) $(CFLAGS) -o $@ $<

install: link2c
	install -Dm755 link2c       $(PREFIX)/bin/link2c
	install -Dm755 link2c_gui.py $(PREFIX)/bin/link2c-gui
	install -Dm755 link2c-recover $(PREFIX)/bin/link2c-recover
	install -d $(PREFIX)/share/applications
	# The graphical session's PATH does not include $(PREFIX)/bin, so the
	# launcher only finds the GUI if the entry spells the path out.
	sed 's|^Exec=link2c-gui$$|Exec=$(PREFIX)/bin/link2c-gui|' link2c.desktop \
		> $(PREFIX)/share/applications/link2c.desktop
	update-desktop-database $(PREFIX)/share/applications 2>/dev/null || true

clean:
	rm -f link2c
.PHONY: install clean
