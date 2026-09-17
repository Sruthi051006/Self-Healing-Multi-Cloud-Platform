"""
Generic microservice used by service-a, service-b and service-c.
The service name and behaviour are driven by environment variables so that
one image can be reused for every service in the demo.

Endpoints
---------
GET  /              -> basic info
GET  /health        -> health probe (returns 503 once the service is "sick")
GET  /metrics       -> cpu / memory / latency / error metrics for the healer
POST /chaos/cpu     -> simulate a runaway CPU leak (for the live demo)
POST /chaos/latency -> simulate a latency spike
POST /chaos/crash   -> simulate a hard crash
POST /chaos/reset   -> back to normal
"""

import os
import random
import threading
import time

from flask import Flask, jsonify

SERVICE_NAME = os.environ.get("SERVICE_NAME", "unknown-service")
SERVICE_ROLE = os.environ.get("SERVICE_ROLE", "generic")
PORT = int(os.environ.get("PORT", "5000"))

app = Flask(__name__)

# ---------------------------------------------------------------- state ----
STARTED_AT = time.time()

state = {
    "mode": "normal",        # normal | cpu | latency | crash
    "requests": 0,
    "errors": 0,
}
_lock = threading.Lock()
_burner_stop = threading.Event()


def _burn_cpu():
    """Spin the CPU to simulate a runaway process."""
    while not _burner_stop.is_set():
        # deliberately wasteful work
        sum(i * i for i in range(20000))


def start_cpu_burn():
    _burner_stop.clear()
    for _ in range(2):
        t = threading.Thread(target=_burn_cpu, daemon=True)
        t.start()


def stop_cpu_burn():
    _burner_stop.set()


# ------------------------------------------------------------- endpoints ----
@app.route("/")
def index():
    return jsonify(
        service=SERVICE_NAME,
        role=SERVICE_ROLE,
        status=state["mode"],
        uptime_seconds=round(time.time() - STARTED_AT, 1),
    )


@app.route("/health")
def health():
    """Kubernetes-style liveness probe."""
    if state["mode"] == "crash":
        return jsonify(service=SERVICE_NAME, healthy=False), 503
    return jsonify(service=SERVICE_NAME, healthy=True), 200


@app.route("/metrics")
def metrics():
    """
    Lightweight metrics endpoint consumed by the healer.

    Real production systems would expose Prometheus format here; we keep it
    as JSON so the demo stays readable.
    """
    with _lock:
        state["requests"] += 1

        mode = state["mode"]
        if mode == "cpu":
            cpu = random.uniform(88, 99)
            latency = random.uniform(120, 260)
        elif mode == "latency":
            cpu = random.uniform(20, 35)
            latency = random.uniform(1400, 2600)
        elif mode == "crash":
            state["errors"] += 1
            cpu = random.uniform(0, 5)
            latency = random.uniform(3000, 5000)
        else:
            cpu = random.uniform(8, 28)
            latency = random.uniform(40, 130)

        error_rate = state["errors"] / max(state["requests"], 1)

        return jsonify(
            service=SERVICE_NAME,
            role=SERVICE_ROLE,
            mode=mode,
            cpu_percent=round(cpu, 2),
            memory_percent=round(random.uniform(30, 55), 2),
            latency_ms=round(latency, 2),
            error_rate=round(error_rate, 4),
            uptime_seconds=round(time.time() - STARTED_AT, 1),
        )


@app.route("/chaos/<kind>", methods=["POST", "GET"])
def chaos(kind):
    """Trigger a failure mode. GET is allowed so it is easy to demo in a browser."""
    kind = kind.lower()
    if kind not in {"cpu", "latency", "crash", "reset"}:
        return jsonify(error="unknown chaos type"), 400

    with _lock:
        if kind == "reset":
            state["mode"] = "normal"
            state["errors"] = 0
            stop_cpu_burn()
        else:
            state["mode"] = kind
            if kind == "cpu":
                start_cpu_burn()

    return jsonify(service=SERVICE_NAME, mode=state["mode"])


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT, threaded=True)
