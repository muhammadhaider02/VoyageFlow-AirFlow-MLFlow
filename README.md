# TitanFlow-AirFlow-MLFlow 🚢

An end-to-end Machine Learning pipeline for **Titanic survival prediction**, orchestrated with **Apache Airflow** and tracked with **MLflow**, running fully locally via **Docker**.

---

## 📁 Project Structure

```
TitanFlow-AirFlow-MLFlow/
├── dags/
│   └── mlops_airflow_mlflow_pipeline.py   ← Pipeline DAG
├── data/
│   └── titanic.csv                        ← Titanic dataset
├── logs/                                  ← Airflow task logs (auto-generated)
├── plugins/                               ← Custom Airflow plugins
├── mlruns/                                ← MLflow experiment data (auto-generated)
├── docker-compose.yml                     ← Spins up Airflow + MLflow + Postgres
├── .env                                   ← Docker environment variables
└── requirements.txt                       ← Python dependencies
```

---

## ⚡ Quick Start

### Step 1 — Get the Dataset
Download `titanic.csv` from Kaggle:
🔗 https://www.kaggle.com/datasets/yasserh/titanic-dataset/data

Place the file at:
```
TitanFlow-AirFlow-MLFlow/data/titanic.csv
```

### Step 2 — Check the `.env` File
The `.env` file is already included and contains two variables that Docker Compose reads automatically:

```env
AIRFLOW_UID=50000          # Airflow container user ID (50000 on Windows/Mac)
AIRFLOW_PROJ_DIR=.         # Points Docker Compose to the current directory
```

You do **not** need to edit this file. It exists so Docker Compose mounts the correct folders.

### Step 3 — Start the Stack
```powershell
docker compose up -d
```

Wait ~2 minutes for all services to initialise (MLflow installs on first boot). Check status:
```powershell
docker compose ps
```
All services should show `(healthy)`.

### Step 4 — Open the UIs

| Service | URL | Credentials |
|---------|-----|-------------|
| **Airflow** | http://localhost:8080 | `airflow` / `airflow` |
| **MLflow**  | http://localhost:5000 | *(no login required)* |

### Step 5 — Run the Pipeline
1. Go to http://localhost:8080
2. Find the DAG: `mlops_airflow_mlflow_pipeline`
3. Toggle it **ON** (blue switch)
4. Click ▶ **Trigger DAG**
5. Watch the Graph View update in real time

### Step 6 — Run 3 Times with Different Hyperparameters
Open `dags/mlops_airflow_mlflow_pipeline.py` and change only **one line** at the top between triggers:

```python
RUN_NUMBER = 1   # ← change to 2, then 3
```

| `RUN_NUMBER` | `C` | `max_iter` | `solver` | Expected result |
|:---:|---|---|---|---|
| `1` | `0.01` | `100` | `lbfgs` | accuracy ~0.65 → model rejected |
| `2` | `1.0` | `200` | `lbfgs` | accuracy ~0.80 → model registered ✅ |
| `3` | `10.0` | `300` | `saga` | accuracy ~0.73 → model rejected |

After each edit, save the file and trigger the DAG again. All 3 runs appear in MLflow for comparison.

### Step 7 — Stop the Stack
```powershell
docker compose down
```

---

## 🔄 DAG Architecture

```
ingest_data
    └── validate_data  ← intentionally fails on attempt #1, retries and succeeds
            ├── handle_missing_age      ─┐
            └── handle_missing_embarked ─┘  (run in PARALLEL)
                        └── encode_features
                                └── train_model  ← logs to MLflow
                                        └── evaluate_model  ← logs metrics to MLflow
                                                └── branch_on_accuracy  ← BranchPythonOperator
                                                      ├── register_model  (accuracy ≥ 0.80)
                                                      └── reject_model    (accuracy < 0.80)
                                                                └── end
```

**Key design features:**
- **Parallel tasks** — `handle_missing_age` and `handle_missing_embarked` run simultaneously
- **Retry mechanism** — `validate_data` intentionally raises an error on attempt #1 to demonstrate Airflow retries
- **Branching** — `BranchPythonOperator` routes to registration or rejection based on accuracy threshold
- **XCom** — tasks pass data (paths, run IDs, metrics) to downstream tasks via Airflow's XCom
- **MLflow integration** — hyperparameters, metrics and model artifacts logged per run

---

## 🛠️ Troubleshooting

| Problem | Fix |
|---------|-----|
| Airflow shows "No DAGs found" | Wait 30s — Airflow rescans every 30 seconds |
| MLflow connection refused | `docker compose ps` — mlflow must show `(healthy)` before triggering DAG |
| `titanic.csv not found` | Confirm file is at `./data/titanic.csv` |
| Port 8080 / 5000 already in use | Stop other apps using those ports |
| `train_model` permission error | Run `docker compose down`, delete `mlruns/`, then `docker compose up -d` |

---

## 🔗 Tech Stack

| Tool | Version | Role |
|------|---------|------|
| Apache Airflow | 2.8.1 (Python 3.11) | Pipeline orchestration |
| MLflow | 2.11.1 | Experiment tracking & model registry |
| scikit-learn | 1.4.x | Model training & evaluation |
| pandas | 2.2.x | Data processing |
| PostgreSQL | 13 | Airflow metadata database |
| Docker Desktop | Latest | Local container runtime |
