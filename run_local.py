"""
Self-Healing Platform - local runner (no Docker required)
=========================================================

Starts the three microservices as ordinary Python processes, then runs the
healer against them. When the healer decides a service is unhealthy it kills
and respawns that process - the same healing behaviour as the container mode,
just with the OS process table instead of the Docker daemon.

Usage:
    pip install -r requirements-local.txt
    python run_local.py

Then open http://localhost:8080
"""

import atexit
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SERVICE_SCRIPT = ROOT / "services" / "app.py"

# service name -> (port, role)
SERVICES = {
    "service-a": (5001, "auth service"),
    "service-b": (5002, "catalog service"),
    "service-c": (5003, "order service"),
}

# point the healer at localhost ports instead of container hostnames
for _name, (_port, _role) in SERVICES.items():
    os.environ[f"{_name.replace('-', '_').upper()}_URL"] = f"http://127.0.0.1:{_port}"
os.environ["RUNTIME"] = "process"

sys.path.insert(0, str(ROOT / "healer"))
import healer  # noqa: E402  (import must follow the env setup above)

# point the healer's service registry at the local ports
healer.SERVICES = {
    name: f"http://127.0.0.1:{port}" for name, (port, _role) in SERVICES.items()
}
healer.history = {name: healer.deque(maxlen=120) for name in healer.SERVICES}
healer.training = {name: [] for name in healer.SERVICES}

# ------------------------------------------------------------- process mgmt --
procs = {}
_spawn_lock = threading.Lock()


def spawn(name):
    """Start one microservice as a subprocess."""
    port, role = SERVICES[name]
    env = dict(os.environ)
    env.update({"SERVICE_NAME": name, "SERVICE_ROLE": role, "PORT": str(port)})
    return subprocess.Popen(
        [sys.executable, str(SERVICE_SCRIPT)],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def restart_process(name):
    """Kill and respawn a service. Wired into the healer as its heal action."""
    with _spawn_lock:
        proc = procs.get(name)
        if proc and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=3)
        time.sleep(0.4)
        procs[name] = spawn(name)
    return True


def shutdown():
    for name, proc in procs.items():
        if proc and proc.poll() is None:
            proc.terminate()


atexit.register(shutdown)


# -------------------------------------------------------------------- main --
def main():
    if not SERVICE_SCRIPT.exists():
        sys.exit(f"Cannot find {SERVICE_SCRIPT}")

    print("Starting microservices...")
    for name in SERVICES:
        procs[name] = spawn(name)
        port = SERVICES[name][0]
        print(f"  {name:12} -> http://127.0.0.1:{port}")

    print("\nWaiting for services to come up...")
    time.sleep(3)

    # hand the healer its restart implementation
    healer.process_restart_hook = restart_process

    print("Starting healer + dashboard...")
    threading.Thread(target=healer.monitor_loop, daemon=True).start()

    print("\n" + "=" * 58)
    print("  Dashboard ready:  http://localhost:8080")
    print("  Detectors need ~40s to learn each service's baseline.")
    print("  Press Ctrl+C to stop everything.")
    print("=" * 58 + "\n")

    try:
        healer.app.run(host="0.0.0.0", port=8080, threaded=True)
    except KeyboardInterrupt:
        pass
    finally:
        shutdown()


if __name__ == "__main__":
    main()
