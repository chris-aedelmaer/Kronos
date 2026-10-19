"""Install the Mac launchd job for this checkout, explicitly paper-only."""
import os
import plistlib
import subprocess
from pathlib import Path

from .forecast import ROOT, STATE

def main():
    if os.uname().sysname != "Darwin":
        raise RuntimeError("This scheduler is macOS-only")
    STATE.mkdir(parents=True, exist_ok=True)
    label = "local.investmentsai.kronos.paper"
    path = Path.home() / "Library/LaunchAgents" / (label + ".plist")
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        raise RuntimeError("Job already exists; inspect it before replacing")
    spec = {
        "Label": label,
        "ProgramArguments": [str(ROOT / ".venv/bin/python"),
                             "-m", "moomoo_integration.runner", "--paper"],
        "WorkingDirectory": str(ROOT),
        "EnvironmentVariables": {"KRONOS_STATE_DIR": str(STATE), "PYTHONUNBUFFERED": "1"},
        "StartInterval": 900,
        "RunAtLoad": False,
        "LowPriorityIO": True,
        "ProcessType": "Background",
        "StandardOutPath": str(STATE / "scheduler.log"),
        "StandardErrorPath": str(STATE / "scheduler-error.log"),
    }
    path.write_bytes(plistlib.dumps(spec))
    target = "gui/" + str(os.getuid())
    subprocess.run(["launchctl", "bootstrap", target, str(path)], check=True)
    print("Installed", label, "every 15 minutes; SIMULATE only")
    print("Stop future orders by creating", STATE / "STOP")

if __name__ == "__main__":
    main()
