#!/bin/sh
# Build dist/gitswitch_<version>_all.deb from the source tree.
set -eu
cd "$(dirname "$0")/.."

version=$(sed -n 's/^__version__ = "\(.*\)"/\1/p' gitswitch/__init__.py)
root=$(mktemp -d)
trap 'rm -rf "$root"' EXIT

install -d "$root/DEBIAN" "$root/usr/bin" "$root/usr/lib/python3/dist-packages/gitswitch/sounds"
install -m 644 gitswitch/*.py "$root/usr/lib/python3/dist-packages/gitswitch/"
install -m 644 gitswitch/sounds/*.wav "$root/usr/lib/python3/dist-packages/gitswitch/sounds/"
install -Dm 644 data/dev.gitswitch.GitSwitch.desktop "$root/usr/share/applications/dev.gitswitch.GitSwitch.desktop"
for catalog in gitswitch/locale/*/LC_MESSAGES/gitswitch.mo; do
    install -Dm 644 "$catalog" "$root/usr/lib/python3/dist-packages/$catalog"
done
icon=icons/hicolor/scalable/apps/dev.gitswitch.GitSwitch.svg
install -Dm 644 "gitswitch/$icon" "$root/usr/lib/python3/dist-packages/gitswitch/$icon"
install -Dm 644 "gitswitch/$icon" "$root/usr/share/$icon"
install -Dm 644 LICENSE "$root/usr/share/doc/gitswitch/copyright"

for entry in "gitswitch:gitswitch.__main__" "gitswitch-mcp:gitswitch.mcp"; do
    printf '#!/usr/bin/python3\nimport sys\nfrom %s import main\nsys.exit(main())\n' "${entry#*:}" > "$root/usr/bin/${entry%%:*}"
    chmod 755 "$root/usr/bin/${entry%%:*}"
done

cat > "$root/DEBIAN/control" <<CONTROL
Package: gitswitch
Version: $version
Architecture: all
Maintainer: gitswitch contributors
Depends: python3 (>= 3.10), python3-gi, gir1.2-gtk-4.0, gir1.2-adw-1 (>= 1.4), gh, git
Recommends: gir1.2-secret-1, pipewire-bin | pulseaudio-utils | alsa-utils
Section: devel
Priority: optional
Description: GUI and MCP server for switching GitHub CLI accounts
 A small GTK window around "gh auth switch" that lists the accounts gh is
 logged in to, shows whether their tokens are still valid and switches
 between them, optionally keeping the global git identity in sync. Also
 manages Forgejo and Gitea accounts with tokens kept in the system keyring.
 Ships an MCP server so coding agents can switch accounts as well.
CONTROL

mkdir -p dist
dpkg-deb --build --root-owner-group "$root" "dist/gitswitch_${version}_all.deb"
