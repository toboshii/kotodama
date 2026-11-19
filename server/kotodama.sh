#!/bin/sh
# Kotodama voice sidecar for Linux and the Steam Deck. Run it from a normal
# host terminal, on its own to start the sidecar or with a command:
#   server/kotodama.sh
#   server/kotodama.sh doctor
#   server/kotodama.sh test --name Ayame --race 2 "Welcome to Norg."
# In its terminal, press R to restart it or Q to quit.
# It uses the Python in server/.venv if you made one, otherwise python3.

here=$(dirname -- "$(readlink -f -- "$0")")
if [ -x "$here/.venv/bin/python" ]; then
    py=$here/.venv/bin/python
elif command -v python3 >/dev/null 2>&1; then
    py=python3
else
    echo "Kotodama needs Python 3.10 or newer, but python3 wasn't found." >&2
    exit 1
fi
# exec: Python takes over this process, so the R and Q keys and systemd's
# stop signal reach it directly.
exec "$py" "$here/kotodama_server.py" "$@"
