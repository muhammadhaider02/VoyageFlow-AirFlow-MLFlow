# DAG Design: Task Dependencies

**DAG ID:** `mlops_airflow_mlflow_pipeline`  
**Model:** Logistic Regression (scikit-learn)  
**Orchestration:** Apache Airflow 2.8 · **Tracking:** MLflow 2.11

---

## Dependency Flow

```
ingest_data
    └── validate_data          ← fails attempt #1 (retry demo), passes attempt #2
            ├── handle_missing_age      ─┐  parallel
            └── handle_missing_embarked ─┘
                        └── encode_features   ← joins both parallel branches
                                └── train_model
                                        └── evaluate_model
                                                └── branch_on_accuracy  (BranchPythonOperator)
                                                      ├── register_model  (accuracy ≥ 0.80)
                                                      └── reject_model    (accuracy < 0.80)
                                                                └── end   (EmptyOperator)
```

---

## Task Dependency Explanation

**`ingest_data` → `validate_data`**  
`validate_data` depends on `ingest_data` completing first. `ingest_data` loads the Titanic CSV and pushes the file path via XCom. Without a confirmed file path, validation cannot begin.

**`validate_data` → `handle_missing_age` + `handle_missing_embarked`** *(fork)*  
Both tasks are triggered simultaneously once validation passes — this is the **parallel fork** point. Declared with `t_validate >> [t_age, t_emb]`. Each task reads the dataset independently using the XCom path from `ingest_data`.

**`handle_missing_age` + `handle_missing_embarked` → `encode_features`** *(join)*  
`encode_features` waits for **both** parallel tasks to finish before starting — this is the **join** point. It reads intermediate pickle files saved by each parallel task and merges them into a single cleaned dataframe. Declared with `[t_age, t_emb] >> t_encode`.

**`encode_features` → `train_model`**  
`train_model` depends on the encoded feature matrix. It pulls the encoded dataframe path via XCom, trains a Logistic Regression model, and logs hyperparameters and the model artifact to MLflow.

**`train_model` → `evaluate_model`**  
`evaluate_model` depends on the trained model. It pulls the MLflow run ID, model pickle, and test data paths via XCom, computes Accuracy / Precision / Recall / F1, and logs all metrics to the same MLflow run.

**`evaluate_model` → `branch_on_accuracy`**  
`branch_on_accuracy` is a **BranchPythonOperator**. It reads accuracy from XCom and returns either `"register_model"` or `"reject_model"` — only one branch executes per run, the other is skipped.

**`branch_on_accuracy` → `register_model` OR `reject_model`**  
Exactly one task runs. `register_model` calls `mlflow.register_model()` when accuracy ≥ 0.80. `reject_model` tags the MLflow run with the rejection reason when accuracy < 0.80.

**(`register_model` OR `reject_model`) → `end`**  
`end` is an `EmptyOperator` with `TriggerRule.NONE_FAILED_MIN_ONE_SUCCESS` — it fires as long as at least one upstream task succeeded and none failed, which is exactly what happens after a branching pattern where one path is always skipped.

---

## No Cyclic Dependencies

The graph flows strictly **top → bottom**. Every arrow points forward — no task appears as both an upstream and downstream dependency of any other task. Airflow enforces this at parse time; a cycle would raise a `DagCycleException` and prevent the DAG from loading. The Airflow UI Graph View (Top → Bottom layout) visually confirms no back-edges exist.

---

## Operators Used

| Task | Operator | Role |
|------|----------|------|
| All data/model tasks | `PythonOperator` | Executes Python functions |
| `branch_on_accuracy` | `BranchPythonOperator` | Returns `task_id` of next branch |
| `end` | `EmptyOperator` | Lightweight post-branch join point |