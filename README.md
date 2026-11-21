# Self-Healing Multi-Cloud Deployment Platform

An autonomous platform that monitors containerised microservices, learns what
"normal" looks like for each one, detects anomalies using unsupervised machine
learning, and automatically restarts unhealthy containers — with zero human
intervention.

---


## Off Campus Verification

Off Campus Code: XWZM95


## The Problem

Modern applications run as dozens of microservices across cloud infrastructure.
When one degrades — a memory leak, a runaway CPU process, a latency spike — the
usual response is manual: an engineer notices an alert, investigates, and
restarts the service. That gap between failure and recovery is downtime, and
downtime is lost revenue and lost users.

Traditional monitoring tools are threshold-based: *"alert me if CPU > 80%."*
Those thresholds are guesses. They fire constantly during normal traffic spikes
and stay silent during genuinely abnormal behaviour that happens to sit below
the line.

## The Solution

This platform replaces static thresholds with a model that learns each
service's actual behavioural baseline at runtime, then acts on deviations
automatically.

```
┌──────────────┐   ┌──────────────┐   ┌──────────────┐
│  service-a   │   │  service-b   │   │  service-c   │
│    (auth)    │   │  (catalog)   │   │   (order)    │
└──────┬───────┘   └──────┬───────┘   └──────┬───────┘
       │ /metrics         │ /metrics         │ /metrics
       └──────────────────┼──────────────────┘
                          ▼
              ┌───────────────────────┐
              │        HEALER         │
              │  ┌─────────────────┐  │
              │  │ Metric Collector│  │  scrapes every 3s
              │  ├─────────────────┤  │
              │  │ IsolationForest │  │  learns baseline, flags anomalies
              │  ├─────────────────┤  │
              │  │  Healing Engine │  │  restarts via Docker API
              │  └─────────────────┘  │
              └───────────┬───────────┘
                          ▼
                  Live Dashboard :8080
```

### How the detection works

1. **Warm-up.** For the first 12 healthy readings, the healer records each
   service's CPU, memory, latency and error rate — building a picture of that
   specific service's normal operating range.
2. **Training.** An `IsolationForest` model is fitted per service on that
   baseline. No pre-trained model is shipped; the system learns the deployment
   it is actually running in.
3. **Detection.** Every subsequent reading is scored. A reading that sits in a
   sparse region of the learned distribution is flagged as anomalous, and the
   feature with the largest z-score deviation is reported as the likely cause.
4. **Healing.** After 2 consecutive anomalous readings (to avoid reacting to
   single noisy samples), the healer restarts that service's container through
   the Docker API, then enters a cooldown before it can heal the same service
   again.

---






## Project Structure

```
self-healing-platform/
├── docker-compose.yml       # orchestrates everything
├── services/
│   ├── app.py               # generic microservice + chaos injection endpoints
│   ├── Dockerfile
│   └── requirements.txt
├── healer/
│   ├── healer.py            # metric collection, ML detection, healing engine
│   ├── static/index.html    # live dashboard
│   ├── Dockerfile
│   └── requirements.txt
└── README.md
```

---

## Configuration

Tunable through environment variables on the `healer` service:

| Variable | Default | Meaning |
|---|---|---|
| `POLL_INTERVAL` | `3` | Seconds between metric scrapes |
| `WARMUP_SAMPLES` | `12` | Healthy readings needed before detection activates |
| `STRIKES_BEFORE_HEAL` | `2` | Consecutive anomalies required to trigger a restart |
| `HEAL_COOLDOWN` | `25` | Seconds before the same service can be healed again |
| `CONTAMINATION` | `0.12` | IsolationForest sensitivity |

---

## Tech Stack

- **Python 3.11** — services and healer
- **Flask** — service APIs and dashboard backend
- **Docker + Docker Compose** — containerisation and orchestration
- **scikit-learn** — IsolationForest anomaly detection
- **Docker SDK** — programmatic container lifecycle control

---


