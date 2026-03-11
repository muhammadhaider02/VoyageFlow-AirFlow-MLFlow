# TitanFlow-AirFlow-MLFlow: End-to-End ML Pipeline

**Project:** Titanic Survival Prediction Pipeline  
**Stack:** Apache Airflow 2.8.1 · MLflow 2.11.1 · scikit-learn 1.4 · Docker Compose  
**Model:** Logistic Regression (3 runs, varying hyperparameters)

---

## 1. Architecture Explanation — Airflow + MLflow Interaction

### System Overview

TitanFlow runs as four Docker containers coordinated by Docker Compose:

| Container | Image | Role |
|-----------|-------|------|
| `airflow-webserver` | `apache/airflow:2.8.1-python3.11` | Web UI and task monitoring |
| `airflow-scheduler` | `apache/airflow:2.8.1-python3.11` | Executes DAG tasks via LocalExecutor |
| `mlflow` | `python:3.11-slim` | Experiment tracking, metric logging, model registry |
| `postgres` | `postgres:13` | Airflow metadata: DAG state, task history, XCom values |

All containers share the same Docker Compose network (`titanflow-dag_default`), allowing them to resolve each other by service name (e.g., `http://mlflow:5000`).

### Airflow → MLflow Interaction

Airflow's **scheduler** executes each task as a subprocess inside the Airflow container. When the pipeline reaches `train_model` and `evaluate_model`, those tasks connect to MLflow's **REST API** over the internal network:

```
Airflow Scheduler Container              MLflow Container (:5000)
┌──────────────────────────┐            ┌──────────────────────────────┐
│  train_model task        │──HTTP──▶   │  Tracking Server             │
│  mlflow.start_run()      │            │  stores run metadata (SQLite) │
│  mlflow.log_param(...)   │            │                              │
│  mlflow.log_model(...)   │──artifact──▶  stores model files in       │
│                          │  (HTTP POST)  /mlruns/artifacts           │
└──────────────────────────┘            └──────────────────────────────┘
```

The MLflow server is started with `--serve-artifacts`, making it proxy all artifact uploads. The Airflow task sends model files via HTTP POST — it never writes to the filesystem directly. This was critical because the Airflow user (UID 50000) does not have write permission to the MLflow volume (`/mlruns`, owned by root).

Task outputs are passed between stages using Airflow's **XCom** (key-value store backed by Postgres): dataset paths, encoded feature paths, MLflow run IDs, model pickle paths, and accuracy scores all flow downstream via XCom pushes and pulls.

---

## 2. DAG Structure and Dependency Explanation

### Pipeline Flow

```
ingest_data
    └── validate_data       ← intentionally fails attempt #1, retries and passes attempt #2
            ├── handle_missing_age      ─┐
            └── handle_missing_embarked ─┘  (run in PARALLEL)
                        └── encode_features   ← joins both parallel branches
                                └── train_model      ← logs params + artifact to MLflow
                                        └── evaluate_model   ← logs accuracy/precision/recall/F1
                                                └── branch_on_accuracy  (BranchPythonOperator)
                                                      ├── register_model  (accuracy ≥ 0.80)
                                                      └── reject_model    (accuracy < 0.80)
                                                                └── end   (EmptyOperator)
```

### Task-by-Task Dependency Explanation

**`ingest_data` → `validate_data`**
`validate_data` depends on `ingest_data` completing first. `ingest_data` loads the Titanic CSV, logs the dataset shape and missing-value counts, and pushes the file path via XCom. Without a confirmed file path, validation cannot begin.

**`validate_data` → `handle_missing_age` + `handle_missing_embarked`** *(parallel fork)*
Both tasks are triggered **simultaneously** once validation passes. Declared with `t_validate >> [t_age, t_emb]`. Each task reads the dataset independently using the XCom path from `ingest_data`. Running them in parallel reduces pipeline time since neither task depends on the other's result.

**`handle_missing_age` + `handle_missing_embarked` → `encode_features`** *(join)*
`encode_features` waits for **both** parallel tasks to complete before starting — neither result alone is sufficient. It reads intermediate pickle files saved by each parallel task and merges them into a single cleaned, encoded dataframe. Declared with `[t_age, t_emb] >> t_encode`.

**`encode_features` → `train_model`**
`train_model` pulls the encoded dataframe path via XCom, trains a Logistic Regression model with the configured hyperparameters, and logs everything to an MLflow run: model type, hyperparameters, dataset size, and the model artifact itself.

**`train_model` → `evaluate_model`**
`evaluate_model` pulls the MLflow run ID, model pickle path, and test data path via XCom. It computes Accuracy, Precision, Recall, and F1-score, logs all four metrics to the same MLflow run, and pushes accuracy via XCom for the branching decision.

**`evaluate_model` → `branch_on_accuracy`**
A **BranchPythonOperator** reads accuracy from XCom and returns either `"register_model"` or `"reject_model"` — only one branch executes per run, the other is automatically marked as **skipped** by Airflow.

**`branch_on_accuracy` → `register_model` OR `reject_model`**
`register_model` calls `mlflow.register_model()` when accuracy ≥ 0.80, creating a versioned entry in the MLflow Model Registry. `reject_model` tags the MLflow run with `model_status=REJECTED` and logs the rejection reason.

**(`register_model` OR `reject_model`) → `end`**
`end` is an `EmptyOperator` with `TriggerRule.NONE_FAILED_MIN_ONE_SUCCESS` — it fires as long as at least one upstream task succeeded and none failed, which is exactly what happens after branching where one path is always skipped.

### No Cyclic Dependencies

The graph flows strictly top → bottom. Airflow enforces acyclicity at parse time — a cycle would raise a `DagCycleException` and prevent the DAG from loading entirely. The Airflow UI Graph View (Top → Bottom layout) visually confirms no back-edges exist.

### Operators Used

| Task | Operator | Role |
|------|----------|------|
| All data/model tasks | `PythonOperator` | Executes Python functions |
| `branch_on_accuracy` | `BranchPythonOperator` | Returns `task_id` of next branch to execute |
| `end` | `EmptyOperator` | Lightweight post-branch join point |

---

## 3. Experiment Comparison Analysis

Three DAG runs were executed, each with a different Logistic Regression configuration set via `RUN_NUMBER` at the top of the DAG file.

### Results

| Run | `C` | `max_iter` | `solver` | Accuracy | Precision | F1-score | Registered |
|-----|-----|-----------|---------|----------|-----------|----------|------------|
| `LR_run1_C0.01_iter100` | 0.01 | 100 | lbfgs | 0.65 | 0.67 | 0.31 | ❌ Rejected |
| **`LR_run2_C1.0_iter200`** | **1.0** | **200** | **lbfgs** | **0.80** | **0.78** | **0.73** | **✅ Registered** |
| `LR_run3_C10.0_iter300` | 10.0 | 300 | saga | 0.73 | 0.83 | 0.51 | ❌ Rejected |

### Analysis

**Run 1 (C=0.01)** applies very strong L2 regularisation, heavily penalising the model's coefficients. This causes severe **underfitting** — the model defaults toward predicting the majority class (non-survival) and achieves a poor F1-score of 0.31, meaning it misses most actual survivors.

**Run 2 (C=1.0)** uses the scikit-learn default regularisation strength. With 200 iterations the solver converges fully. It is the only run to cross the 80% accuracy threshold and achieves the highest F1-score of 0.73, indicating it correctly classifies both survivors and non-survivors.

**Run 3 (C=10.0)** applies weak regularisation with the `saga` solver. Despite the highest precision (0.83), its F1-score drops to 0.51 — high precision but low recall means the model is too conservative, rarely predicting survival and missing many actual survivors.

### Best Model

**`LR_run2_C1.0_iter200`** is the best-performing model. The Titanic dataset is imbalanced (~62% non-survival, ~38% survival), so F1-score is the most meaningful metric — accuracy alone can be inflated by simply predicting the majority class. Run 2 leads all three runs in both accuracy and F1-score, representing the optimal balance between underfitting (Run 1) and overfitting risk (Run 3). It was automatically registered in the MLflow Model Registry as `TitanFlow-Titanic-Survival-Model Version 1`.

---

## 4. Failure and Retry Explanation

### Development Failures Summary

Seven distinct failures were encountered and resolved during implementation:

| # | Task | Error | Root Cause | Fix |
|---|------|-------|-----------|-----|
| 1 | `airflow-init` | `ModuleNotFoundError: No module named 'airflow'` | Running as root (UID 0) couldn't access packages installed for the `airflow` user | Removed `user: "0:0"` and custom entrypoint; used official Airflow entrypoint |
| 2 | `airflow-init` | `No matching distribution: scikit-learn==1.4` | `apache/airflow:2.8.1` uses Python 3.8; scikit-learn 1.4+ requires Python 3.9+ | Changed image to `apache/airflow:2.8.1-python3.11` |
| 3 | `validate_data` | Always failed, never retried successfully | Module-level counter resets each subprocess; always appeared as attempt #1 | Replaced with `context['ti'].try_number` |
| 4 | `train_model` | `[Errno 111] Connection refused` to `mlflow:5000` | YAML `>` folded scalar caused `--host 0.0.0.0` to run as a separate shell command; MLflow bound to `127.0.0.1` only | Switched to YAML list format for command |
| 5 | `train_model` | `PermissionError: /mlruns` | MLflow client tried writing artifacts directly to `/mlruns` (root-owned) | Added `--serve-artifacts`; artifacts now uploaded via HTTP |
| 6 | `train_model` | `PermissionError: /mlruns` (persisted) | Existing experiment had stale `artifact_location: file:///mlruns/...` baked in from before `--serve-artifacts` | Wiped `mlruns/` so experiment recreated with `mlflow-artifacts://` URI |
| 7 | `evaluate_model` | `ValueError: mix of unknown and binary targets` | `y_test` (pandas Series, non-contiguous index) vs `y_pred` (numpy array) classified differently by sklearn's `type_of_target()` | Cast both to `np.array(...).ravel().astype(int)` |

### Intentional Retry Demonstration

`validate_data` is explicitly designed to fail on its first attempt, demonstrating Airflow's built-in retry mechanism:

```python
attempt = context["ti"].try_number   # 1 on first run, 2 on retry
if attempt == 1:
    raise RuntimeError("[INTENTIONAL] Validation failed — Airflow will retry.")
# Real validation logic runs from attempt 2 onwards
```

The task is configured with `retries=2` and `retry_delay=timedelta(seconds=10)`. The Airflow event log clearly shows the cycle: `validate_data` → **failed** → *(10s wait)* → **running** → **success**, all within the same DAG run. This proves Airflow's retry mechanism is active without requiring any external failure.

---

## 5. Reflection — Production Deployment

### 5.1 Scaling the Executor

The current setup uses **LocalExecutor**, which runs tasks sequentially as subprocesses on a single machine. For production with concurrent DAGs and large datasets:

- **CeleryExecutor** distributes tasks across a cluster of worker nodes via a message broker (Redis/RabbitMQ), enabling horizontal scaling.
- **KubernetesExecutor** spins up a fresh Pod per task, providing perfect isolation, resource allocation per task type, and elastic scaling — ideal for variable ML workloads.

### 5.2 Cloud-Native Storage

The pipeline currently uses local volume mounts (`./data`, `/tmp/titanflow`, `./mlruns`). In production:

- Raw data should be stored in **Amazon S3**, **Google Cloud Storage**, or **Azure Blob Storage**, and referenced by URI rather than local path.
- MLflow's `--artifacts-destination` should point to an S3 bucket (`s3://bucket/mlruns`), making artifacts durable, versioned, and accessible to all workers regardless of host machine.
- Intermediate pickle files (`/tmp/titanflow/`) should use distributed object storage or be replaced with XCom for small payloads.

### 5.3 Database

MLflow currently stores experiment metadata in local SQLite files. For concurrent multi-user production use, this should be replaced with a managed **PostgreSQL** or **MySQL** instance, capable of handling concurrent reads and writes from multiple Airflow workers.

### 5.4 Secrets Management

The pipeline uses hardcoded credentials (`airflow/airflow`) and plain `.env` files. In production:

- All credentials should be stored in **AWS Secrets Manager**, **HashiCorp Vault**, or **Kubernetes Secrets**.
- Airflow **Connections** should be used for external service credentials rather than environment variables.
- `.env` files must never be committed to version control.

### 5.5 Monitoring and Alerting

`email_on_failure=False` is set for the development environment. In production:

- Failure alerts should be delivered via email, Slack, or PagerDuty using Airflow's notification hooks.
- Task duration metrics should be exported to a monitoring stack (Prometheus + Grafana) to detect pipeline regressions or model accuracy drift over time.

### 5.6 Model Versioning and Promotion

The current pipeline registers a new model version on every successful run. In production, a **staged promotion workflow** would be required:

- New versions enter `Staging`, are evaluated on a holdout set or canary traffic, and only promoted to `Production` after passing defined quality gates.
- MLflow's Model Registry natively supports `Staging → Production → Archived` lifecycle transitions, enabling safe rollback if a new model degrades in production.