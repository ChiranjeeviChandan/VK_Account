#!/usr/bin/env bash
# Installs Felix for the current user (Linux and macOS): a private Python
# environment and a `felix` command. Run it again after `git pull` to update.
#
#   FELIX_PREFIX   where the environment goes   (default ~/.local/share/felix-app)
#   FELIX_BIN      where the command is linked  (default ~/.local/bin)
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
prefix="${FELIX_PREFIX:-$HOME/.local/share/felix-app}"
bin="${FELIX_BIN:-$HOME/.local/bin}"

python=""
for candidate in python3.13 python3.12 python3.11 python3.10 python3; do
  if command -v "$candidate" >/dev/null 2>&1 &&
     "$candidate" -c 'import sys; raise SystemExit(sys.version_info < (3, 10))' 2>/dev/null; then
    python="$candidate"
    break
  fi
done
if [ -z "$python" ]; then
  echo "Felix needs Python 3.10 or newer. Get it from https://www.python.org/downloads/ and run this again." >&2
  exit 1
fi

echo "Installing Felix with $("$python" --version) into $prefix"
if ! "$python" -m venv "$prefix" 2>/dev/null; then
  echo "Python could not create an environment. On Debian or Ubuntu run: sudo apt install python3-venv" >&2
  exit 1
fi
"$prefix/bin/python" -m pip install --quiet --upgrade pip
"$prefix/bin/python" -m pip install --quiet --upgrade "$here"

mkdir -p "$bin"
ln -sf "$prefix/bin/felix" "$bin/felix"
echo "Installed $("$prefix/bin/felix" --version)."

case ":$PATH:" in
  *":$bin:"*) ;;
  *) echo "Note: $bin is not on your PATH. Add this line to your shell profile:"
     echo "  export PATH=\"$bin:\$PATH\"" ;;
esac

cat <<'NEXT'

Next:
  felix demo      look around with sample data
  felix setup     connect your own accounts
  felix start     run Felix and open the dashboard
NEXT
