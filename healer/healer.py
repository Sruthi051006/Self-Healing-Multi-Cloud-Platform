"""
Self-Healing Platform - Healer
==============================

The healer is the brain of the platform. Every few seconds it:

  1. scrapes /metrics from every registered microservice
  2. feeds those metrics into an IsolationForest anomaly detector
  3. when a service is flagged as anomalous for N consecutive checks,
     it restarts that service's container through the Docker API
  4. records every observation and action so the dashboard can show
     what happened and why

The detector trains itself on the first WARMUP_SAMPLES healthy readings,
so there is no pre-trained model to ship - it learns what "normal" looks
like for this particular deployment at runtime.
"""

import os
import threading
import time
from collections import defaultdict, deque
from datetime import datetime, timezone

import numpy as np
import requests
from flask import Flask, jsonify, send_from_directory
from sklearn.ensemble import IsolationForest

# ------------------------------------------------------------------ config --
POLL_INTERVAL = float(os.environ.get("POLL_INTERVAL", "3"))
WARMUP_SAMPLES = int(os.environ.get("WARMUP_SAMPLES", "20"))
STRIKES_BEFORE_HEAL = int(os.environ.get("STRIKES_BEFORE_HEAL", "2"))
HEAL_COOLDOWN = float(os.environ.get("HEAL_COOLDOWN", "25"))
CONTAMINATION = float(os.environ.get("CONTAMINATION", "0.05"))
# how many standard deviations from the learned mean a feature must be
# before an IsolationForest flag is acted on (guards false positives)
Z_THRESHOLD = float(os.environ.get("Z_THRESHOLD", "3.5"))

# service name -> base url  (container name is assumed to equal service name)
SERVICES = {
    "service-a": os.environ.get("SERVICE_A_URL", "http://service-a:5000"),
    "service-b": os.environ.get("SERVICE_B_URL", "http://service-b:5000"),
    "service-c": os.environ.get("SERVICE_C_URL", "http://service-c:5000"),
}

FEATURES = ("cpu_percent", "memory_percent", "latency_ms", "error_rate")

# A baseline built from only WARMUP_SAMPLES readings can, by chance, have an
# unrealistically tight standard deviation for a feature - which then makes
# perfectly normal readings look like multi-sigma outliers. These floors stop
# that: no feature's "normal spread" is ever treated as narrower than this.
MIN_STD = {
    "cpu_percent": 6.0,
    "memory_percent": 6.0,
    "latency_ms": 20.0,
    "error_rate": 0.05,
}

# ------------------------------------------------------------------- state --
history = {name: deque(maxlen=120) for name in SERVICES}       # recent readings
training = {name: [] for name in SERVICES}                      # warmup buffer
models = {}                                                     # name -> model
strikes = defaultdict(int)
last_heal = defaultdict(float)
heal_count = defaultdict(int)
events = deque(maxlen=200)
current = {}                                                    # latest snapshot

_lock = threading.Lock()


def log_event(service, kind, message):
    entry = {
        "time": datetime.now(timezone.utc).strftime("%H:%M:%S"),
        "service": service,
        "kind": kind,          # info | anomaly | heal | error
        "message": message,
    }
    with _lock:
        events.appendleft(entry)
    print(f"[{entry['time']}] {kind.upper():8} {service:12} {message}", flush=True)


# ----------------------------------------------------------- heal backends --
# The healer can manage services either as Docker containers or as plain OS
# processes. The runtime is selected with the RUNTIME environment variable so
# the same detection logic works in both modes.
RUNTIME = os.environ.get("RUNTIME", "docker")

# run_local.py injects a callable here when RUNTIME == "process"
process_restart_hook = None

docker_client = None


def get_docker():
    try:
        import docker

        client = docker.from_env()
        client.ping()
        return client
    except Exception as exc:  # pragma: no cover - environment dependent
        log_event("healer", "error", f"Docker API unavailable: {exc}")
        return None


def restart_container(service):
    """Restart the unit backing a service. Returns True on success."""
    if RUNTIME == "process":
        if process_restart_hook is None:
            log_event(service, "error", "No process restart hook registered")
            return False
        try:
            return bool(process_restart_hook(service))
        except Exception as exc:
            log_event(service, "error", f"Restart failed: {exc}")
            return False

    global docker_client
    if docker_client is None:
        docker_client = get_docker()
    if docker_client is None:
        log_event(service, "error", "Cannot heal - Docker API not reachable")
        return False
    try:
        container = docker_client.containers.get(service)
        container.restart(timeout=5)
        return True
    except Exception as exc:
        log_event(service, "error", f"Restart failed: {exc}")
        return False


# ---------------------------------------------------------------- detection --
def vectorise(reading):
    return [float(reading.get(f, 0.0)) for f in FEATURES]


def train_model(service):
    data = np.array(training[service])
    model = IsolationForest(
        n_estimators=100,
        contamination=CONTAMINATION,
        random_state=42,
    )
    model.fit(data)
    models[service] = model
    log_event(
        service,
        "info",
        f"Baseline learned from {len(data)} samples - anomaly detection active",
    )


def is_anomalous(service, reading):
    """
    Returns (anomalous: bool, score: float, reason: str)

    Two independent signals are used:
      * the unsupervised IsolationForest score
      * a hard health-check failure, which is always an anomaly
    """
    if not reading.get("healthy", True):
        return True, -1.0, "health check failing"

    model = models.get(service)
    if model is None:
        return False, 0.0, "still learning baseline"

    vec = np.array([vectorise(reading)])
    score = float(model.decision_function(vec)[0])
    flagged = model.predict(vec)[0] == -1

    if not flagged:
        return False, score, "normal"

    # The IsolationForest alone is too eager: `contamination` forces it to
    # treat a fixed share of readings as outliers, so on healthy-but-noisy
    # metrics it produces false positives. We therefore require a second,
    # independent signal - the reading must also sit well outside the
    # baseline's own spread on at least one feature before we act on it.
    base = np.array(training[service])
    means = base.mean(axis=0)
    stds = base.std(axis=0)
    floor = np.array([MIN_STD[f] for f in FEATURES])
    stds = np.maximum(stds, floor)
    z = np.abs((np.array(vectorise(reading)) - means) / stds)
    worst_idx = int(np.argmax(z))

    if z[worst_idx] < Z_THRESHOLD:
        return False, score, "normal"

    worst = FEATURES[worst_idx]
    return (
        True,
        score,
        f"{worst.replace('_', ' ')} outside learned baseline "
        f"({z[worst_idx]:.1f}σ)",
    )


# -------------------------------------------------------------- monitor loop --
def scrape(service, url):
    reading = {"service": service, "reachable": True, "healthy": True}
    try:
        m = requests.get(f"{url}/metrics", timeout=3).json()
        reading.update(m)
    except Exception:
        reading.update(
            {
                "reachable": False,
                "healthy": False,
                "cpu_percent": 0.0,
                "memory_percent": 0.0,
                "latency_ms": 5000.0,
                "error_rate": 1.0,
                "mode": "unreachable",
            }
        )
        return reading

    try:
        h = requests.get(f"{url}/health", timeout=3)
        reading["healthy"] = h.status_code == 200
    except Exception:
        reading["healthy"] = False

    return reading


def monitor_loop():
    log_event("healer", "info", "Healer started - collecting baseline metrics")
    while True:
        for service, url in SERVICES.items():
            reading = scrape(service, url)
            anomalous, score, reason = is_anomalous(service, reading)

            reading["anomaly_score"] = round(score, 4)
            reading["anomalous"] = anomalous
            reading["reason"] = reason
            reading["heals"] = heal_count[service]
            reading["checked_at"] = datetime.now(timezone.utc).strftime("%H:%M:%S")

            with _lock:
                current[service] = reading
                history[service].append(
                    {
                        "t": reading["checked_at"],
                        "cpu": reading.get("cpu_percent", 0),
                        "latency": reading.get("latency_ms", 0),
                        "anomalous": anomalous,
                    }
                )

            # collect a clean baseline before the model exists
            if service not in models:
                if reading["reachable"] and reading["healthy"]:
                    training[service].append(vectorise(reading))
                    if len(training[service]) >= WARMUP_SAMPLES:
                        train_model(service)
                continue

            if anomalous:
                strikes[service] += 1
                log_event(
                    service,
                    "anomaly",
                    f"Anomaly {strikes[service]}/{STRIKES_BEFORE_HEAL} - {reason} "
                    f"(cpu {reading.get('cpu_percent')}%, "
                    f"latency {reading.get('latency_ms')}ms)",
                )
            else:
                if strikes[service]:
                    log_event(service, "info", "Recovered - metrics back to normal")
                strikes[service] = 0

            # ---- heal ----
            cooled = (time.time() - last_heal[service]) > HEAL_COOLDOWN
            if strikes[service] >= STRIKES_BEFORE_HEAL and cooled:
                log_event(service, "heal", "Threshold crossed - restarting container")
                if restart_container(service):
                    heal_count[service] += 1
                    last_heal[service] = time.time()
                    strikes[service] = 0
                    log_event(
                        service,
                        "heal",
                        f"Container restarted successfully "
                        f"(total heals: {heal_count[service]})",
                    )

        time.sleep(POLL_INTERVAL)


# ---------------------------------------------------------------------- api --
app = Flask(__name__, static_folder="static")


@app.route("/")
def dashboard():
    return send_from_directory("static", "index.html")


@app.route("/api/status")
def api_status():
    with _lock:
        return jsonify(
            services=[current[s] for s in SERVICES if s in current],
            events=list(events)[:40],
            trained=[s for s in SERVICES if s in models],
            total_heals=sum(heal_count.values()),
        )


@app.route("/api/history/<service>")
def api_history(service):
    with _lock:
        return jsonify(list(history.get(service, [])))


@app.route("/api/chaos/<service>/<kind>", methods=["POST"])
def api_chaos(service, kind):
    """Proxy chaos injection so the dashboard can trigger failures."""
    url = SERVICES.get(service)
    if not url:
        return jsonify(error="unknown service"), 404
    try:
        r = requests.post(f"{url}/chaos/{kind}", timeout=3)
        log_event(service, "info", f"Chaos injected: {kind}")
        return jsonify(r.json())
    except Exception as exc:
        return jsonify(error=str(exc)), 500


if __name__ == "__main__":
    threading.Thread(target=monitor_loop, daemon=True).start()
    app.run(host="0.0.0.0", port=8080, threaded=True)
