#!/usr/bin/env python3
"""Generate and install a systemd user service for the Telegram bridge."""

import pathlib
import sys

SERVICE_TEMPLATE = """[Unit]
Description=OpenCode Telegram Bridge
PartOf=opencode-bridge.target

[Service]
Type=simple
WorkingDirectory={bridge_dir}
ExecStart={bridge_dir}/venv/bin/python -m bot
Environment=PYTHONUNBUFFERED=1
Restart=always
RestartSec=5
TimeoutStopSec=120

[Install]
WantedBy=opencode-bridge.target
"""

TARGET_TEMPLATE = """[Unit]
Description=OpenCode Bridge (TUI + Telegram)
PartOf=default.target

[Unit]
ConditionHost=localhost

[Install]
WantedBy=default.target
"""


def main():
    bridge_dir = pathlib.Path(__file__).parent.resolve()
    config_dir = pathlib.Path.home() / ".config" / "systemd" / "user"

    target_file = config_dir / "opencode-bridge.target"
    target_file.parent.mkdir(parents=True, exist_ok=True)

    print(f"Writing {target_file}")
    target_file.write_text(TARGET_TEMPLATE)

    service_file = config_dir / "opencode-bridge-telegram.service"
    service_content = SERVICE_TEMPLATE.format(bridge_dir=bridge_dir)

    print(f"Writing {service_file}")
    service_file.write_text(service_content)

    print("\nTo enable and start:")
    print("  systemctl --user daemon-reload")
    print("  systemctl --user enable --now opencode-bridge-telegram.service")
    print("  systemctl --user enable --now opencode-bridge.target")
    print("\nTo view logs:")
    print("  journalctl --user -u opencode-bridge-telegram.service -f")
    print("\nTo start opencode serve separately:")
    print("  opencode serve --port 4096")


if __name__ == "__main__":
    main()