# FAILURE.md — Issues Encountered & Fixes Applied

---

## Failure 1 — `airflow-init` container crashed on startup

**Error:**
```
service "airflow-init" didn't complete successfully: exit 1
ModuleNotFoundError: No module named 'airflow'
```
**Root Cause:**
The `docker-compose.yml` originally used `user: "0:0"` and a custom `entrypoint: /bin/bash` on the `airflow-init` service. Running as root broke Python's module resolution — Airflow is installed under the `airflow` user's local path (`~/.local`), which root cannot see.

**Fix:**
Removed `user: "0:0"` and `entrypoint: /bin/bash`. Switched to `command: version` and let the official Airflow entrypoint handle DB migration and user creation automatically via the environment variables `_AIRFLOW_DB_MIGRATE` and `_AIRFLOW_WWW_USER_CREATE`.

---

## Failure 2 — `airflow-init` still crashed after Fix 1

**Error:**
```
ERROR: No matching distribution found for scikit-learn==1.4.1.post1
```
**Root Cause:**
`apache/airflow:2.8.1` defaults to **Python 3.8**, which is incompatible with `scikit-learn >= 1.4` (requires Python 3.9+).

**Fix:**
Changed the Docker image from `apache/airflow:2.8.1` → `apache/airflow:2.8.1-python3.11`, matching the Python version used in the MLflow container.

---

## Failure 3 — `validate_data` permanently failing (never retrying successfully)

**Error:**
Every attempt of `validate_data` raised:
```
RuntimeError: [INTENTIONAL] Validation failed on first attempt — Airflow will retry.
```
even on attempt 2 and beyond, causing the entire pipeline to be stuck in `upstream_failed`.

**Root Cause:**
The retry counter was a **module-level Python variable** (`_validation_attempt = 0`). Airflow's LocalExecutor launches each task attempt as a **fresh subprocess**, so the module is re-imported and the counter resets to `0` every single time — making it always look like the first attempt.

**Fix:**
Replaced the module-level counter with `context['ti'].try_number` — Airflow's built-in per-task attempt counter that correctly reflects `1` on the first attempt, `2` on the first retry, etc.

```python
# Before (broken)
_validation_attempt += 1
if _validation_attempt == 1:
    raise RuntimeError(...)

# After (fixed)
attempt = context["ti"].try_number
if attempt == 1:
    raise RuntimeError(...)
```

---

## Retry Behavior (Post-Fix)

| Attempt | `try_number` | Outcome |
|---------|-------------|---------|
| 1st run | 1 | ❌ Intentional `RuntimeError` raised |
| *(10s wait — Airflow retry delay)* | — | — |
| 2nd run | 2 | ✅ Real validation logic runs, passes |

---

## Failure 4 — `train_model` failed: MLflow connection refused

**Error (from task logs):**
```
HTTPConnectionPool(host='mlflow', port=5000):
Failed to establish a new connection: [Errno 111] Connection refused
```

**Root Cause — YAML multi-line command parsing bug:**

The `docker-compose.yml` used a YAML `>` folded block scalar for the MLflow container command:

```yaml
# BROKEN — folded scalar folds to single string, but bash treats each
# line after a newline as a NEW command, not a continuation argument
command: >
  bash -c "pip install mlflow==2.11.1 &&
           mlflow server
           --host 0.0.0.0      ← treated as separate shell command!
           --port 5000          ← treated as separate shell command!
           ..."
```

Bash received `mlflow server` with **no arguments** on its own line, so it started with its default bind address: `127.0.0.1:5000` (localhost only). The `--host 0.0.0.0` flag was silently ignored because it ran as a separate (failing) command.

Confirmed by inspecting the running process inside the mlflow container:
```
/proc/168/cmdline → python3.11 -m gunicorn -b 127.0.0.1:5000 mlflow.server:app
```

Since MLflow only listened on `127.0.0.1` (its own loopback), no other Docker container could connect to it — hence `Connection refused`.

**Fix:** Switched to YAML **list format** for the command, which passes each element as an exact argument with zero shell ambiguity:

```yaml
# FIXED — list format, bash receives one unambiguous script string
command:
  - bash
  - -c
  - "pip install mlflow==2.11.1 && mlflow server --host 0.0.0.0 --port 5000 --backend-store-uri /mlruns --default-artifact-root /mlruns"
```

Also fixed two related issues:
- MLflow **healthcheck** used `curl` which is not installed in `python:3.11-slim` → replaced with Python's built-in `urllib.request`
- Airflow webserver and scheduler now declare `mlflow: condition: service_healthy` in `depends_on` — guaranteeing MLflow is ready before any Airflow tasks can run

---

## Failure 5 — `train_model` failed: Permission denied on `/mlruns`

**Error (from task logs):**
```
PermissionError: [Errno 13] Permission denied: '/mlruns'
```

**Root Cause — MLflow artifact client writes directly to disk:**

MLflow has two modes for artifact storage:
- **Without `--serve-artifacts`**: The MLflow server only stores run *metadata*. Artifact files are written **directly to the artifact root path by the client** (the Airflow task). Since `/mlruns` is owned by the MLflow container's root user, the Airflow user (UID 50000) has no write permission.
- **With `--serve-artifacts`**: The MLflow server proxies all artifact uploads — the client sends files over HTTP to the server, and the server writes them to disk. The client never touches the filesystem directly.

Our MLflow server was started with `--default-artifact-root /mlruns` but **without `--serve-artifacts`**, so every `mlflow.sklearn.log_model()` call in the Airflow task tried to write model files directly to `/mlruns` and was rejected by the OS.

**Fix:** Added `--serve-artifacts` and changed `--default-artifact-root` to `--artifacts-destination` in the MLflow server command:

```yaml
# Before (broken) — client writes artifacts directly to /mlruns
- "pip install mlflow==2.11.1 && mlflow server --host 0.0.0.0 --port 5000 \
   --backend-store-uri /mlruns --default-artifact-root /mlruns"

# After (fixed) — server proxies all artifact uploads over HTTP
- "pip install mlflow==2.11.1 && mlflow server --host 0.0.0.0 --port 5000 \
   --backend-store-uri /mlruns --artifacts-destination /mlruns --serve-artifacts"
```

With `--serve-artifacts`, the Airflow task uploads model artifacts via HTTP POST to `http://mlflow:5000`. The MLflow server (running as root) receives them and writes to `/mlruns`. No direct filesystem access required from Airflow.

---

## Failure 6 — `train_model` still failing: `PermissionError /mlruns` persists after `--serve-artifacts`

**Error (from task logs — full traceback):**
```
File "mlflow/store/artifact/local_artifact_repo.py", line 60, in log_artifacts
    mkdir(artifact_dir)
PermissionError: [Errno 13] Permission denied: '/mlruns'
```

**Root Cause — Stale experiment `artifact_location` in MLflow database:**

Even after enabling `--serve-artifacts`, the existing `TitanFlow-Titanic-Survival` experiment in the MLflow database retained its original `artifact_location`:
```
artifact_location: /mlruns/1   ← file URI, set before --serve-artifacts was added
```

MLflow clients inherit the experiment's `artifact_location` for every new run. Since the URI was `file:///mlruns/...`, the client selected `LocalArtifactRepo` and attempted to write directly to `/mlruns`, **bypassing the HTTP proxy entirely**. The `--serve-artifacts` flag only applies to experiments created **after** it was enabled.

**Confirmed by:**
- MLflow traceback shows `local_artifact_repo.py` is being used (not the HTTP repo)
- The MLflow UI screenshot showed the run was created (connectivity works), but it failed in 5 seconds — exactly when `log_model` tried to write artifacts locally

**Fix:** Stopped the MLflow container, wiped the `./mlruns/` directory on the host to remove all stale experiment metadata, then restarted. When the DAG runs again, `mlflow.set_experiment()` will create a **fresh experiment** whose `artifact_location` is `mlflow-artifacts:/...` (the proxy URI), so all artifact uploads go through the server via HTTP.

```powershell
docker compose stop mlflow
Remove-Item -Recurse -Force mlruns\*
docker compose up -d --force-recreate mlflow
```

---

## Failure 7 — `evaluate_model` failed: sklearn type mismatch

**Error (from task logs):**
```
ValueError: Classification metrics can't handle a mix of unknown and binary targets
  at: accuracy_score(y_test, y_pred)
```

**Root Cause:**

`y_test` is a **pandas Series** with a non-contiguous integer index (a subset of the original DataFrame rows after `train_test_split`). `y_pred` is a **numpy array**. sklearn's internal `type_of_target()` function inspects the object type and index and can classify one as `"binary"` and the other as `"unknown"` — triggering the ValueError even though both contain only `{0, 1}` values.

**Fix:** Explicitly cast both arrays to flat 1-D numpy int arrays before computing any metric:

```python
# Before (broken)
accuracy = accuracy_score(y_test, y_pred)

# After (fixed)
y_test_arr = np.array(y_test).ravel().astype(int)
y_pred_arr = np.array(y_pred).ravel().astype(int)
accuracy   = accuracy_score(y_test_arr, y_pred_arr)
```

`.ravel()` ensures 1-D shape. `.astype(int)` ensures consistent integer dtype. Diagnostic logging was also added to print `dtype`, `shape`, and `unique values` of both arrays so future type issues are immediately visible in the task logs.
