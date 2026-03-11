"""
TitanFlow-DAG: End-to-End ML Pipeline
=======================================
Author  : TitanFlow Team
Dataset : Titanic (Kaggle)
Tools   : Apache Airflow 2.8 + MLflow 2.x

DAG Structure (simplified):
    ingest_data
        └── validate_data
                ├── handle_missing_age   (parallel)
                ├── handle_missing_emb   (parallel)
                │         └── encode_features (join)
                │                  └── train_model
                │                          └── evaluate_model
                │                                  └── branch_on_accuracy
                │                                        ├── register_model
                │                                        └── reject_model
"""

import os
import logging
import pickle
import pandas as pd
import numpy as np

from datetime import datetime, timedelta

from airflow import DAG
from airflow.operators.python import PythonOperator, BranchPythonOperator
from airflow.operators.empty import EmptyOperator
from airflow.utils.trigger_rule import TriggerRule

import mlflow
import mlflow.sklearn
from mlflow.models.signature import infer_signature

from sklearn.linear_model import LogisticRegression
from sklearn.model_selection import train_test_split
from sklearn.preprocessing import LabelEncoder
from sklearn.metrics import (
    accuracy_score,
    precision_score,
    recall_score,
    f1_score,
)

# ──────────────────────────────────────────────────────────────
# CONSTANTS  — change HYPERPARAMS between runs for Task 10
# ──────────────────────────────────────────────────────────────

# Path inside the Airflow container (mounted from ./data on the host)
DATASET_PATH = "/opt/airflow/data/titanic.csv"

# MLflow tracking server (Docker service name resolves internally)
MLFLOW_TRACKING_URI = os.getenv("MLFLOW_TRACKING_URI", "http://mlflow:5000")
EXPERIMENT_NAME = "TitanFlow-Titanic-Survival"

# ── Task 10: Change RUN_NUMBER (1, 2, or 3) between DAG triggers ─
#
#   Run 1 → C=0.01, max_iter=100, solver='lbfgs'
#   Run 2 → C=1.0,  max_iter=200, solver='lbfgs'
#   Run 3 → C=10.0, max_iter=300, solver='saga'
#
RUN_NUMBER = 3

_RUN_CONFIGS = {
    1: {"C": 0.01, "max_iter": 100, "solver": "lbfgs",  "random_state": 42},
    2: {"C": 1.0,  "max_iter": 200, "solver": "lbfgs",  "random_state": 42},
    3: {"C": 10.0, "max_iter": 300, "solver": "saga",   "random_state": 42},
}

MODEL_TYPE = "LogisticRegression"
HYPERPARAMS = _RUN_CONFIGS[RUN_NUMBER]

# Accuracy threshold for model registration
ACCURACY_THRESHOLD = 0.80

# Shared temp directory inside the container
TMP_DIR = "/tmp/titanflow"

# ──────────────────────────────────────────────────────────────
# DEFAULT DAG ARGUMENTS
# ──────────────────────────────────────────────────────────────

default_args = {
    "owner": "titanflow",
    "depends_on_past": False,
    "start_date": datetime(2024, 1, 1),
    "email_on_failure": False,
    "email_on_retry": False,
    "retries": 2,                         # Task 3 uses this for retry demo
    "retry_delay": timedelta(seconds=10),
}

# ──────────────────────────────────────────────────────────────
# TASK DEFINITIONS
# ──────────────────────────────────────────────────────────────


# ── TASK 2 : Data Ingestion ────────────────────────────────
def ingest_data(**context):
    """
    Load Titanic CSV → log shape + missing value counts → push path via XCom.
    """
    logging.info("=" * 60)
    logging.info("TASK 2 — Data Ingestion")
    logging.info("=" * 60)

    if not os.path.exists(DATASET_PATH):
        raise FileNotFoundError(
            f"Dataset not found at {DATASET_PATH}. "
            "Please place titanic.csv in the ./data folder."
        )

    df = pd.read_csv(DATASET_PATH)

    # Print dataset shape
    logging.info(f"Dataset Shape   : {df.shape[0]} rows × {df.shape[1]} columns")
    logging.info(f"Columns         : {list(df.columns)}")

    # Log missing value counts per column
    missing = df.isnull().sum()
    logging.info("Missing Values per Column:")
    for col, count in missing.items():
        if count > 0:
            pct = count / len(df) * 100
            logging.info(f"  {col:<15}: {count:>4} missing  ({pct:.1f}%)")

    logging.info(f"Dataset loaded successfully from {DATASET_PATH}")

    # Push dataset path via XCom so downstream tasks can pick it up
    context["ti"].xcom_push(key="dataset_path", value=DATASET_PATH)


# ── TASK 3 : Data Validation ──────────────────────────────
def validate_data(**context):
    """
    Check if Age or Embarked missing percentage exceeds 30%.
    Raises an exception on the FIRST attempt (try_number == 1) to demonstrate
    Airflow's retry mechanism.  Uses context['ti'].try_number which is the
    correct Airflow-native way to detect which attempt is running — unlike a
    module-level counter, this survives across subprocess restarts.
    """
    ti = context["ti"]
    attempt = ti.try_number          # 1 on first run, 2 on first retry, etc.

    logging.info("=" * 60)
    logging.info("TASK 3 — Data Validation")
    logging.info("=" * 60)
    logging.info(f"Validation attempt #{attempt} (try_number from Airflow context)")

    dataset_path = context["ti"].xcom_pull(
        task_ids="ingest_data", key="dataset_path"
    )
    df = pd.read_csv(dataset_path)

    # ── Intentional failure on first attempt (retry demo) ──
    if attempt == 1:
        logging.warning(
            "Simulating a transient failure on attempt #1 "
            "to demonstrate Airflow retry behavior."
        )
        raise RuntimeError(
            "[INTENTIONAL] Validation failed on first attempt — Airflow will retry."
        )

    # ── Real validation logic (runs from attempt #2 onwards) ─
    cols_to_check = ["Age", "Embarked"]
    for col in cols_to_check:
        missing_pct = df[col].isnull().mean() * 100
        logging.info(f"  {col}: {missing_pct:.2f}% missing")
        if missing_pct > 30:
            raise ValueError(
                f"Column '{col}' has {missing_pct:.1f}% missing values — "
                "exceeds the 30% threshold. Aborting pipeline."
            )

    logging.info("Validation passed — missing percentages are within limits.")



# ── TASK 4a : Handle Missing Age ──────────────────────────
def handle_missing_age(**context):
    """
    Fill missing Age values with the median age.
    Runs in PARALLEL with handle_missing_embarked.
    Saves intermediate result to /tmp for feature-engineering join.
    """
    logging.info("=" * 60)
    logging.info("TASK 4a — Handle Missing Age (Parallel Branch)")
    logging.info("=" * 60)

    dataset_path = context["ti"].xcom_pull(
        task_ids="ingest_data", key="dataset_path"
    )
    df = pd.read_csv(dataset_path)

    before = df["Age"].isnull().sum()
    df["Age"] = df["Age"].fillna(df["Age"].median())
    after = df["Age"].isnull().sum()
    logging.info(f"Age  — filled {before} missing values with median ({df['Age'].median():.1f}).")
    logging.info(f"Age  — remaining nulls after fill: {after}")

    # ── Feature Engineering: FamilySize & IsAlone ─────────
    df["FamilySize"] = df["SibSp"] + df["Parch"] + 1
    df["IsAlone"] = (df["FamilySize"] == 1).astype(int)
    logging.info("Feature Engineering: 'FamilySize' and 'IsAlone' columns created.")

    # Save to temp file for the join task
    os.makedirs(TMP_DIR, exist_ok=True)
    tmp_path = os.path.join(TMP_DIR, "df_age.pkl")
    df.to_pickle(tmp_path)
    logging.info(f"Intermediate dataframe saved to {tmp_path}")

    context["ti"].xcom_push(key="age_tmp_path", value=tmp_path)


# ── TASK 4b : Handle Missing Embarked ─────────────────────
def handle_missing_embarked(**context):
    """
    Fill missing Embarked values with the mode.
    Runs in PARALLEL with handle_missing_age.
    """
    logging.info("=" * 60)
    logging.info("TASK 4b — Handle Missing Embarked (Parallel Branch)")
    logging.info("=" * 60)

    dataset_path = context["ti"].xcom_pull(
        task_ids="ingest_data", key="dataset_path"
    )
    df = pd.read_csv(dataset_path)

    before = df["Embarked"].isnull().sum()
    mode_val = df["Embarked"].mode()[0]
    df["Embarked"] = df["Embarked"].fillna(mode_val)
    after = df["Embarked"].isnull().sum()
    logging.info(f"Embarked — filled {before} missing values with mode ('{mode_val}').")
    logging.info(f"Embarked — remaining nulls after fill: {after}")

    # Save to temp file
    os.makedirs(TMP_DIR, exist_ok=True)
    tmp_path = os.path.join(TMP_DIR, "df_emb.pkl")
    df.to_pickle(tmp_path)
    logging.info(f"Intermediate dataframe saved to {tmp_path}")

    context["ti"].xcom_push(key="emb_tmp_path", value=tmp_path)


# ── TASK 5 : Data Encoding ────────────────────────────────
def encode_features(**context):
    """
    Merge results from the two parallel tasks, encode categorical variables,
    drop irrelevant columns, and save the final feature matrix.
    """
    logging.info("=" * 60)
    logging.info("TASK 5 — Data Encoding (join after parallel tasks)")
    logging.info("=" * 60)

    # Pull paths from both parallel branches
    age_path = context["ti"].xcom_pull(
        task_ids="handle_missing_age", key="age_tmp_path"
    )
    emb_path = context["ti"].xcom_pull(
        task_ids="handle_missing_embarked", key="emb_tmp_path"
    )

    df_age = pd.read_pickle(age_path)
    df_emb = pd.read_pickle(emb_path)

    # Merge: take FamilySize/IsAlone from age-branch, Embarked fill from emb-branch
    df = df_age.copy()
    df["Embarked"] = df_emb["Embarked"]

    # Encode Sex → 0/1
    le = LabelEncoder()
    df["Sex"] = le.fit_transform(df["Sex"])           # male=1, female=0
    logging.info("Encoded 'Sex' column (male=1, female=0).")

    # Encode Embarked → 0/1/2
    df["Embarked"] = le.fit_transform(df["Embarked"])
    logging.info("Encoded 'Embarked' column (C=0, Q=1, S=2).")

    # Drop irrelevant / high-cardinality columns
    drop_cols = ["PassengerId", "Name", "Ticket", "Cabin"]
    df.drop(columns=drop_cols, inplace=True, errors="ignore")
    logging.info(f"Dropped columns: {drop_cols}")

    # Drop any remaining rows with nulls in features
    df.dropna(inplace=True)
    logging.info(f"Final dataframe shape after encoding: {df.shape}")

    # Save encoded dataframe
    os.makedirs(TMP_DIR, exist_ok=True)
    enc_path = os.path.join(TMP_DIR, "df_encoded.pkl")
    df.to_pickle(enc_path)

    context["ti"].xcom_push(key="encoded_path", value=enc_path)
    context["ti"].xcom_push(key="dataset_size", value=df.shape[0])


# ── TASK 6 : Model Training with MLflow ───────────────────
def train_model(**context):
    """
    Train a Logistic Regression model and log everything to MLflow.
    Change RUN_NUMBER at the top of the file for each of the 3 Task-10 runs.
    """
    logging.info("=" * 60)
    logging.info("TASK 6 — Model Training with MLflow")
    logging.info("=" * 60)

    enc_path = context["ti"].xcom_pull(
        task_ids="encode_features", key="encoded_path"
    )
    dataset_size = context["ti"].xcom_pull(
        task_ids="encode_features", key="dataset_size"
    )

    df = pd.read_pickle(enc_path)
    X = df.drop("Survived", axis=1)
    y = df["Survived"]

    X_train, X_test, y_train, y_test = train_test_split(
        X, y, test_size=0.2, random_state=42, stratify=y
    )

    # ── MLflow Setup ───────────────────────────────────────
    mlflow.set_tracking_uri(MLFLOW_TRACKING_URI)
    mlflow.set_experiment(EXPERIMENT_NAME)

    run_name = f"LR_run{RUN_NUMBER}_C{HYPERPARAMS['C']}_iter{HYPERPARAMS['max_iter']}"
    with mlflow.start_run(run_name=run_name) as run:
        run_id = run.info.run_id
        logging.info(f"MLflow Run ID : {run_id}")
        logging.info(f"Run Name      : {run_name}")

        # Log model type, run number and all hyperparameters
        mlflow.log_param("model_type",   MODEL_TYPE)
        mlflow.log_param("run_number",   RUN_NUMBER)
        mlflow.log_param("dataset_size", dataset_size)
        for param_name, param_val in HYPERPARAMS.items():
            mlflow.log_param(param_name, param_val)

        # ── Build and train LogisticRegression ─────────────
        model = LogisticRegression(
            C=HYPERPARAMS["C"],
            max_iter=HYPERPARAMS["max_iter"],
            solver=HYPERPARAMS["solver"],
            random_state=HYPERPARAMS["random_state"],
        )
        model.fit(X_train, y_train)
        logging.info(f"LogisticRegression trained — C={HYPERPARAMS['C']}, "
                     f"max_iter={HYPERPARAMS['max_iter']}, solver={HYPERPARAMS['solver']}")

        # Log model artifact to MLflow
        signature = infer_signature(X_train, model.predict(X_train))
        mlflow.sklearn.log_model(
            model,
            artifact_path="model",
            signature=signature,
            registered_model_name=None,   # registration happens in Task 9
        )
        logging.info("Model artifact logged to MLflow.")

        # Log dataset size
        mlflow.log_param("train_size", X_train.shape[0])
        mlflow.log_param("test_size", X_test.shape[0])

    # Persist model + test data to tmp for Task 7
    os.makedirs(TMP_DIR, exist_ok=True)
    model_path = os.path.join(TMP_DIR, "model.pkl")
    with open(model_path, "wb") as f:
        pickle.dump(model, f)

    test_data_path = os.path.join(TMP_DIR, "test_data.pkl")
    with open(test_data_path, "wb") as f:
        pickle.dump((X_test, y_test), f)

    context["ti"].xcom_push(key="run_id", value=run_id)
    context["ti"].xcom_push(key="model_path", value=model_path)
    context["ti"].xcom_push(key="test_data_path", value=test_data_path)


# ── TASK 7 : Model Evaluation ─────────────────────────────
def evaluate_model(**context):
    """
    Compute Accuracy, Precision, Recall and F1 — log all to MLflow.
    Push accuracy via XCom for the branching task.
    """
    logging.info("=" * 60)
    logging.info("TASK 7 — Model Evaluation")
    logging.info("=" * 60)

    run_id = context["ti"].xcom_pull(task_ids="train_model", key="run_id")
    model_path = context["ti"].xcom_pull(task_ids="train_model", key="model_path")
    test_data_path = context["ti"].xcom_pull(task_ids="train_model", key="test_data_path")

    with open(model_path, "rb") as f:
        model = pickle.load(f)
    with open(test_data_path, "rb") as f:
        X_test, y_test = pickle.load(f)

    y_pred = model.predict(X_test)

    # ── Normalise to flat 1-D numpy int arrays ─────────────
    # y_test is a pandas Series (non-contiguous index after train_test_split).
    # y_pred is a numpy array.  sklearn's type_of_target() can classify one as
    # "binary" and the other as "unknown" causing a ValueError.
    # Explicit conversion to flat int arrays avoids this entirely.
    y_test_arr = np.array(y_test).ravel().astype(int)
    y_pred_arr = np.array(y_pred).ravel().astype(int)

    logging.info(f"y_test dtype={y_test_arr.dtype}, shape={y_test_arr.shape}, "
                 f"unique={set(y_test_arr.tolist())}")
    logging.info(f"y_pred dtype={y_pred_arr.dtype}, shape={y_pred_arr.shape}, "
                 f"unique={set(y_pred_arr.tolist())}")

    accuracy  = accuracy_score(y_test_arr, y_pred_arr)
    precision = precision_score(y_test_arr, y_pred_arr, zero_division=0)
    recall    = recall_score(y_test_arr, y_pred_arr, zero_division=0)
    f1        = f1_score(y_test_arr, y_pred_arr, zero_division=0)

    logging.info(f"  Accuracy  : {accuracy:.4f}")
    logging.info(f"  Precision : {precision:.4f}")
    logging.info(f"  Recall    : {recall:.4f}")
    logging.info(f"  F1-Score  : {f1:.4f}")

    # Log all metrics to the existing MLflow run
    mlflow.set_tracking_uri(MLFLOW_TRACKING_URI)
    with mlflow.start_run(run_id=run_id):
        mlflow.log_metric("accuracy",  accuracy)
        mlflow.log_metric("precision", precision)
        mlflow.log_metric("recall",    recall)
        mlflow.log_metric("f1_score",  f1)

    logging.info("All metrics logged to MLflow.")

    # Push accuracy for branching decision (Task 8)
    context["ti"].xcom_push(key="accuracy", value=accuracy)
    context["ti"].xcom_push(key="run_id",   value=run_id)


# ── TASK 8 : Branching Logic ──────────────────────────────
def branch_on_accuracy(**context):
    """
    Route the pipeline:
      accuracy >= ACCURACY_THRESHOLD → register_model
      otherwise                      → reject_model
    """
    accuracy = context["ti"].xcom_pull(
        task_ids="evaluate_model", key="accuracy"
    )
    logging.info(f"Branching decision — Accuracy: {accuracy:.4f}, Threshold: {ACCURACY_THRESHOLD}")

    if accuracy >= ACCURACY_THRESHOLD:
        logging.info("→ Accuracy meets threshold. Routing to REGISTER MODEL.")
        return "register_model"
    else:
        logging.info("→ Accuracy below threshold. Routing to REJECT MODEL.")
        return "reject_model"


# ── TASK 9a : Model Registration ──────────────────────────
def register_model(**context):
    """
    Register the approved model in the MLflow Model Registry.
    """
    logging.info("=" * 60)
    logging.info("TASK 9a — Model Registration (Accuracy ≥ threshold)")
    logging.info("=" * 60)

    run_id   = context["ti"].xcom_pull(task_ids="evaluate_model", key="run_id")
    accuracy = context["ti"].xcom_pull(task_ids="evaluate_model", key="accuracy")

    mlflow.set_tracking_uri(MLFLOW_TRACKING_URI)

    model_uri = f"runs:/{run_id}/model"
    registered = mlflow.register_model(
        model_uri=model_uri,
        name="TitanFlow-Titanic-Survival-Model",
    )

    logging.info(f"Model registered: name='{registered.name}', version={registered.version}")
    logging.info(f"Registered model URI : {model_uri}")
    logging.info(f"Achieved accuracy    : {accuracy:.4f}")


# ── TASK 9b : Model Rejection ─────────────────────────────
def reject_model(**context):
    """
    Log the rejection reason when accuracy is below the threshold.
    """
    logging.info("=" * 60)
    logging.info("TASK 9b — Model Rejected (Accuracy < threshold)")
    logging.info("=" * 60)

    run_id   = context["ti"].xcom_pull(task_ids="evaluate_model", key="run_id")
    accuracy = context["ti"].xcom_pull(task_ids="evaluate_model", key="accuracy")

    reason = (
        f"Model rejected: accuracy {accuracy:.4f} is below the "
        f"required threshold of {ACCURACY_THRESHOLD}."
    )
    logging.warning(reason)

    mlflow.set_tracking_uri(MLFLOW_TRACKING_URI)
    with mlflow.start_run(run_id=run_id):
        mlflow.set_tag("rejection_reason", reason)
        mlflow.set_tag("model_status", "REJECTED")

    logging.info("Rejection reason tagged in MLflow run.")


# ──────────────────────────────────────────────────────────────
# DAG DEFINITION — Task 1
# ──────────────────────────────────────────────────────────────

with DAG(
    dag_id="mlops_airflow_mlflow_pipeline",
    description=(
        "End-to-end Titanic survival prediction pipeline: "
        "Airflow orchestration + MLflow experiment tracking."
    ),
    default_args=default_args,
    # Run manually / on-demand (no schedule for the assignment)
    schedule="@once",
    catchup=False,
    tags=["mlops", "titanic", "mlflow", "airflow"],
) as dag:

    # ── Task 2 ──────────────────────────────────────────────
    t_ingest = PythonOperator(
        task_id="ingest_data",
        python_callable=ingest_data,
    )

    # ── Task 3 (retries=2 inherited from default_args) ──────
    t_validate = PythonOperator(
        task_id="validate_data",
        python_callable=validate_data,
        retries=2,
        retry_delay=timedelta(seconds=10),
    )

    # ── Task 4a — parallel ──────────────────────────────────
    t_age = PythonOperator(
        task_id="handle_missing_age",
        python_callable=handle_missing_age,
    )

    # ── Task 4b — parallel ──────────────────────────────────
    t_emb = PythonOperator(
        task_id="handle_missing_embarked",
        python_callable=handle_missing_embarked,
    )

    # ── Task 5 — joins the two parallel branches ────────────
    t_encode = PythonOperator(
        task_id="encode_features",
        python_callable=encode_features,
    )

    # ── Task 6 ──────────────────────────────────────────────
    t_train = PythonOperator(
        task_id="train_model",
        python_callable=train_model,
    )

    # ── Task 7 ──────────────────────────────────────────────
    t_evaluate = PythonOperator(
        task_id="evaluate_model",
        python_callable=evaluate_model,
    )

    # ── Task 8 — BranchPythonOperator ───────────────────────
    t_branch = BranchPythonOperator(
        task_id="branch_on_accuracy",
        python_callable=branch_on_accuracy,
    )

    # ── Task 9a ─────────────────────────────────────────────
    t_register = PythonOperator(
        task_id="register_model",
        python_callable=register_model,
    )

    # ── Task 9b ─────────────────────────────────────────────
    t_reject = PythonOperator(
        task_id="reject_model",
        python_callable=reject_model,
    )

    # ── End marker (TriggerRule.NONE_FAILED_MIN_ONE_SUCCESS
    #    means it fires whichever branch completed) ──────────
    t_end = EmptyOperator(
        task_id="end",
        trigger_rule=TriggerRule.NONE_FAILED_MIN_ONE_SUCCESS,
    )

    t_ingest >> t_validate >> [t_age, t_emb] >> t_encode
    t_encode >> t_train >> t_evaluate >> t_branch
    t_branch >> [t_register, t_reject]
    [t_register, t_reject] >> t_end