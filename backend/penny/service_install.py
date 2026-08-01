"""Install ``penny daemon`` as a user service (launchd / systemd --user).

``penny init`` calls :func:`install_and_start`; ``penny daemon
install|start|stop|status`` are the manual controls. macOS gets a launchd
agent in ``~/Library/LaunchAgents``; Linux a systemd user unit in
``~/.config/systemd/user``. Other platforms: run ``penny daemon run``
under your own supervisor.
"""

# Every subprocess call here is a fixed launchctl/systemctl argv — no user
# input reaches it.
# ruff: noqa: S603, S607

from __future__ import annotations

from pathlib import Path
import platform
import shlex
import shutil
import subprocess
import sys

_LABEL = "com.penny.daemon"
_UNIT = "penny-daemon.service"


def penny_argv() -> list[str]:
    """Argv that invokes penny: the console script, or a ``-m penny.cli`` fallback.

    The canonical form is a list — each consumer renders it (plist array,
    shell-quoted systemd line, subprocess argv), so paths with spaces survive.
    """
    found = shutil.which("penny")
    if found:
        return [found]
    return [sys.executable, "-m", "penny.cli"]


def _launchd_plist(log_dir: Path) -> str:
    args = "\n".join(
        f"        <string>{part}</string>" for part in [*penny_argv(), "daemon", "run"]
    )
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>{_LABEL}</string>
    <key>ProgramArguments</key>
    <array>
{args}
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>{log_dir}/daemon.out.log</string>
    <key>StandardErrorPath</key>
    <string>{log_dir}/daemon.err.log</string>
</dict>
</plist>
"""


def _systemd_unit() -> str:
    return f"""[Unit]
Description=Penny daemon (sync + scheduled reports)

[Service]
ExecStart={shlex.join([*penny_argv(), "daemon", "run"])}
Restart=on-failure

[Install]
WantedBy=default.target
"""


def install_and_start(log_dir: Path) -> str:
    """Install + start the daemon service. Returns a human status line."""
    system = platform.system()
    log_dir.mkdir(parents=True, exist_ok=True)
    if system == "Darwin":
        plist = Path.home() / "Library" / "LaunchAgents" / f"{_LABEL}.plist"
        plist.parent.mkdir(parents=True, exist_ok=True)
        plist.write_text(_launchd_plist(log_dir), encoding="utf-8")
        subprocess.run(["launchctl", "unload", str(plist)], capture_output=True)
        subprocess.run(["launchctl", "load", str(plist)], check=True)
        return f"launchd agent installed + loaded: {plist}"
    if system == "Linux":
        unit = Path.home() / ".config" / "systemd" / "user" / _UNIT
        unit.parent.mkdir(parents=True, exist_ok=True)
        unit.write_text(_systemd_unit(), encoding="utf-8")
        subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
        subprocess.run(["systemctl", "--user", "enable", "--now", _UNIT], check=True)
        return f"systemd user unit installed + started: {unit}"
    return (
        f"unsupported platform {system!r} — run `penny daemon run` under your "
        "own supervisor"
    )


def stop() -> str:
    system = platform.system()
    if system == "Darwin":
        plist = Path.home() / "Library" / "LaunchAgents" / f"{_LABEL}.plist"
        subprocess.run(["launchctl", "unload", str(plist)], capture_output=True)
        return "launchd agent unloaded"
    if system == "Linux":
        subprocess.run(["systemctl", "--user", "stop", _UNIT], capture_output=True)
        return "systemd user unit stopped"
    return f"unsupported platform {system!r}"


def is_running() -> bool:
    system = platform.system()
    if system == "Darwin":
        out = subprocess.run(
            ["launchctl", "list"], capture_output=True, text=True
        ).stdout
        return _LABEL in out
    if system == "Linux":
        code = subprocess.run(
            ["systemctl", "--user", "is-active", "--quiet", _UNIT],
            capture_output=True,
        ).returncode
        return code == 0
    return False
