# Best Performing Model

**Experiment:** `TitanFlow-Titanic-Survival`  
**Model type:** Logistic Regression 
**Evaluation metric:** Accuracy (primary) · Precision · Recall · F1-score

---

## Run Comparison

| Run | `C` | `max_iter` | `solver` | Accuracy | Precision | Recall | F1-score | Registered? |
|-----|-----|-----------|---------|----------|-----------|--------|----------|-------------|
| `LR_run1_C0.01_iter100` | 0.01 | 100 | lbfgs | 0.65 | 0.67 | — | 0.31 | ❌ Rejected |
| **`LR_run2_C1.0_iter200`** | **1.0** | **200** | **lbfgs** | **0.80** | **0.78** | — | **0.73** | **✅ Registered** |
| `LR_run3_C10.0_iter300` | 10.0 | 300 | saga | 0.73 | 0.83 | — | 0.51 | ❌ Rejected |

---

## Best Model: `LR_run2_C1.0_iter200`

**Run 2** with `C=1.0`, `max_iter=200`, `solver=lbfgs` is the best-performing model.

### Why C=1.0 wins

The regularization parameter `C` controls the **inverse of regularization strength**:
- **C=0.01 (Run 1):** Very strong regularization — the model is heavily penalized for complex decision boundaries. It underfits, achieving only 65% accuracy and an F1 of 0.31. It essentially predicts the majority class too often.
- **C=1.0 (Run 2):** Balanced regularization — the model fits the training data well without overfitting. It achieves 80% accuracy and a strong F1 of 0.73, indicating it correctly identifies both survivors and non-survivors.
- **C=10.0 (Run 3):** Weak regularization — the model is allowed too much freedom. Accuracy drops back to 73% despite higher precision (0.83), suggesting it became overly conservative in predicting survivors (high precision, but lower recall), hurting the F1 score significantly (0.51).

### Why F1 matters here

The Titanic dataset is **imbalanced** (~62% did not survive, ~38% survived). Accuracy alone can be misleading — a model that always predicts "not survived" would achieve ~62%. F1-score balances precision and recall, making it the more meaningful metric for this problem.

Run 2 achieves the best **F1-score of 0.73**, confirming it not only reaches the 80% accuracy threshold but also predicts survivors reliably.

### Conclusion

> **`LR_run2_C1.0_iter200`** — `C=1.0`, default `lbfgs` solver — is the optimal configuration. It represents the sweet spot between underfitting (Run 1) and overfitting risk (Run 3), delivering the highest accuracy and F1-score across all experiments. It was automatically registered in the MLflow Model Registry as `TitanFlow-Titanic-Survival-Model v1`.
