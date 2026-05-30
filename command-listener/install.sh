#!/bin/bash
set -e

DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$DIR"

# Create venv with uv
if [ ! -d .venv ]; then
    uv venv .venv --python 3.11
fi

# Install package + deps
uv pip install -e . --python .venv/bin/python

# Install user-level systemd unit
mkdir -p ~/.config/systemd/user
cat > ~/.config/systemd/user/command-listener.service << EOF
[Unit]
Description=Discord Command Listener
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=$DIR
ExecStart=$DIR/.venv/bin/python -m command_listener.main
Restart=always
RestartSec=5
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=default.target
EOF

systemctl --user daemon-reload
systemctl --user enable command-listener
systemctl --user restart command-listener
echo "✅ command-listener service installed and started"
echo "   Logs:    journalctl --user -u command-listener -f"
echo "   Status:  systemctl --user status command-listener"
