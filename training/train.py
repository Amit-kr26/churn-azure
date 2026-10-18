import argparse
import datetime
import hashlib
import json
import logging
import os
import pickle
from pathlib import Path

import joblib
import numpy as np
import pandas as pd
from sklearn.calibration import CalibratedClassifierCV
from sklearn.compose import ColumnTransformer
from sklearn.ensemble import (
    ExtraTreesClassifier,
    HistGradientBoostingClassifier,
    RandomForestClassifier,
)
from sklearn.inspection import permutation_importance
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import (
    accuracy_score,
    brier_score_loss,
    confusion_matrix,
    f1_score,
    recall_score,
    roc_auc_score,
)
from sklearn.model_selection import StratifiedKFold, cross_val_predict
from sklearn.neural_network import MLPClassifier
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import OneHotEncoder, StandardScaler

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger("train")

REPO_ROOT = Path(__file__).resolve().parents[1]
DATA_PATH = REPO_ROOT / "data" / "raw" / "churn.csv"
MODEL_DIR = REPO_ROOT / "api" / "model"

TARGET = "Exited"
DROP_COLS = ["RowNumber", "CustomerId", "Surname"]
CATEGORICAL = ["Geography", "Gender"]
NUMERIC = [
    "CreditScore",
    "Age",
    "Tenure",
    "Balance",
    "NumOfProducts",
    "HasCrCard",
    "IsActiveMember",
    "EstimatedSalary",
]
FEATURES = NUMERIC + CATEGORICAL


def generate_synthetic(n: int = 1000, seed: int = 42) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    geos = rng.choice(["France", "Spain", "Germany"], size=n, p=[0.5, 0.25, 0.25])
    genders = rng.choice(["Male", "Female"], size=n, p=[0.55, 0.45])
    credit = rng.integers(350, 851, size=n)
    age = rng.integers(18, 93, size=n)
    active = rng.integers(0, 2, size=n)
    logit = (
        -0.5
        + 1.2 * (credit < 600)
        + 1.0 * (age > 50)
        + 0.8 * (active == 0)
        + rng.normal(0, 0.8, size=n)
    )
    exited = (logit > 0.6).astype(int)
    return pd.DataFrame(
        {
            "RowNumber": range(1, n + 1),
            "CustomerId": rng.integers(15_000_000, 16_000_000, size=n),
            "Surname": [f"Synth{i}" for i in range(n)],
            "CreditScore": credit,
            "Geography": geos,
            "Gender": genders,
            "Age": age,
            "Tenure": rng.integers(0, 11, size=n),
            "Balance": rng.uniform(0, 250_000, size=n).round(2),
            "NumOfProducts": rng.integers(1, 5, size=n),
            "HasCrCard": rng.integers(0, 2, size=n),
            "IsActiveMember": active,
            "EstimatedSalary": rng.uniform(10_000, 200_000, size=n).round(2),
            "Exited": exited,
        }
    )


REQUIRED = {TARGET, "CustomerId", *FEATURES}


def read_valid_csvs(folder: Path) -> list[pd.DataFrame]:
    frames = []
    for path in sorted(folder.rglob("*.csv")):
        frame = pd.read_csv(path)
        missing = REQUIRED - set(frame.columns)
        if missing:
            logger.warning("Skipping %s: missing columns %s", path.name, sorted(missing))
            continue
        frames.append(frame)
    return frames


def load_data(data_path: Path, allow_synthetic: bool) -> tuple[pd.DataFrame, str]:
    if data_path.is_dir():
        frames = read_valid_csvs(data_path)
        if not frames:
            raise SystemExit(f"no usable CSV files under {data_path}")
        logger.info("Loaded %d CSV file(s) from %s", len(frames), data_path)
        df = pd.concat(frames, ignore_index=True).drop_duplicates(subset="CustomerId", keep="last")
        data_source = "real"
    elif data_path.exists():
        logger.info("Loading %s", data_path)
        df = pd.read_csv(data_path)
        data_source = "real"
    elif allow_synthetic:
        logger.warning("%s not found; generating synthetic 1000-row dataset.", data_path)
        df = generate_synthetic(1000)
        data_source = "synthetic"
    else:
        raise SystemExit(
            f"{data_path} not found. Pass --allow-synthetic to train on a "
            "clearly-marked synthetic dataset, or provide the real CSV."
        )
    missing = REQUIRED - set(df.columns)
    if missing:
        raise ValueError(f"Dataset missing columns: {sorted(missing)}")
    return df, data_source


def holdout_mask(df: pd.DataFrame) -> np.ndarray:
    return pd.util.hash_pandas_object(df["CustomerId"], index=False).to_numpy() % 5 == 0


def recall_gap(X: pd.DataFrame, y, pred) -> float:
    worst = 0.0
    for col in ("Geography", "Gender"):
        recalls = []
        for value in X[col].unique():
            mask = (X[col] == value).to_numpy() & (np.asarray(y) == 1)
            if mask.sum() >= 20:
                recalls.append(float(np.asarray(pred)[mask].mean()))
        if len(recalls) > 1:
            worst = max(worst, max(recalls) - min(recalls))
    return worst


CANDIDATES = {
    "logistic_regression": lambda: LogisticRegression(max_iter=2000),
    "random_forest": lambda: RandomForestClassifier(
        n_estimators=300, min_samples_leaf=5, n_jobs=-1, random_state=42
    ),
    "extra_trees": lambda: ExtraTreesClassifier(
        n_estimators=300, min_samples_leaf=5, n_jobs=-1, random_state=42
    ),
    "gradient_boosting": lambda: HistGradientBoostingClassifier(
        max_depth=4, learning_rate=0.06, max_iter=250, random_state=42
    ),
    "neural_network": lambda: MLPClassifier(
        hidden_layer_sizes=(32, 16), alpha=1e-3, early_stopping=True, max_iter=500, random_state=42
    ),
}
MAX_GAP_INCREASE = 0.10
THRESHOLDS = np.round(np.arange(0.05, 0.96, 0.01), 2)


def build_model(name: str, n_rows: int) -> CalibratedClassifierCV:
    pre = ColumnTransformer(
        [
            ("num", StandardScaler(), NUMERIC),
            ("cat", OneHotEncoder(handle_unknown="ignore", sparse_output=False), CATEGORICAL),
        ]
    )
    pipe = Pipeline([("preprocess", pre), ("clf", CANDIDATES[name]())])
    return CalibratedClassifierCV(pipe, method="isotonic" if n_rows >= 1500 else "sigmoid", cv=5)


def best_threshold(y_true, proba) -> tuple[float, float]:
    scores = [f1_score(y_true, proba >= t, zero_division=0) for t in THRESHOLDS]
    i = int(np.argmax(scores))
    return float(THRESHOLDS[i]), float(scores[i])


def evaluate(name: str, X_train, y_train, X_test, y_test) -> dict:
    folds = StratifiedKFold(n_splits=5, shuffle=True, random_state=42)
    oof = cross_val_predict(build_model(name, len(X_train)), X_train, y_train, cv=folds, method="predict_proba")[:, 1]
    threshold, cv_f1 = best_threshold(y_train, oof)
    model = build_model(name, len(X_train)).fit(X_train, y_train)
    proba = model.predict_proba(X_test)[:, 1]
    pred = proba >= threshold
    return {
        "name": name,
        "threshold": threshold,
        "cv_f1": round(cv_f1, 4),
        "cv_auc": round(float(roc_auc_score(y_train, oof)), 4),
        "test_f1": round(float(f1_score(y_test, pred)), 4),
        "test_auc": round(float(roc_auc_score(y_test, proba)), 4),
        "test_accuracy": round(float(accuracy_score(y_test, pred)), 4),
        "model": model,
    }


def importances(model, X_test, y_test) -> list:
    result = permutation_importance(model, X_test, y_test, scoring="roc_auc", n_repeats=5, random_state=42)
    raw = np.clip(result.importances_mean, 0, None)
    total = float(raw.sum()) or 1.0
    ranked = sorted(zip(FEATURES, raw / total, strict=True), key=lambda t: t[1], reverse=True)
    return [{"feature": f, "importance": round(float(v), 4)} for f, v in ranked]


def current_scores(current: Path | None, X_test, y_test) -> dict | None:
    if current is None or not (current / "churn_model.pkl").exists():
        return None
    try:
        model = joblib.load(current / "churn_model.pkl")
        threshold = json.loads((current / "columns.json").read_text())["threshold"]
        pred = model.predict_proba(X_test)[:, 1] >= threshold
    except (OSError, ValueError, KeyError, AttributeError, ImportError, pickle.UnpicklingError) as exc:
        logger.warning("Could not score the current model (%s); publishing the new one.", type(exc).__name__)
        return None
    return {"f1": float(f1_score(y_test, pred)), "gap": recall_gap(X_test, y_test, pred)}


def reference_row(X_train: pd.DataFrame) -> dict:
    ref = {c: float(X_train[c].median()) for c in NUMERIC}
    ref.update({c: X_train[c].mode().iloc[0] for c in CATEGORICAL})
    return {k: (int(v) if k in NUMERIC and float(v).is_integer() else v) for k, v in ref.items()}


def parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Train the bank churn model.")
    parser.add_argument("--data", type=Path, default=DATA_PATH, help="Churn CSV or a folder of CSVs.")
    parser.add_argument("--out", type=Path, default=MODEL_DIR, help="Artifact output directory.")
    parser.add_argument(
        "--current",
        type=Path,
        default=None,
        help="Folder with the model in service. The new model is only written if it scores at least as well on the held-out rows and does not widen the recall gap between groups.",
    )
    parser.add_argument(
        "--models",
        nargs="+",
        choices=list(CANDIDATES),
        default=list(CANDIDATES),
        help="Candidate models to compare.",
    )
    parser.add_argument(
        "--allow-synthetic",
        action="store_true",
        help="Generate a clearly-marked synthetic dataset when the CSV is missing.",
    )
    parser.add_argument(
        "--model-version",
        default=os.getenv("MODEL_VERSION") or datetime.datetime.now(datetime.timezone.utc).strftime("%Y.%m.%d.%H%M"),
        help="Release label written into the artifacts.",
    )
    return parser.parse_args(argv)


ARTIFACT_FILES = ("churn_model.pkl", "columns.json", "metrics.json", "stats.json")


def hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def write_manifest(out_dir: Path, *, model_version: str, data_source: str, n_rows: int) -> Path:
    manifest = {
        "model_version": model_version,
        "data_source": data_source,
        "n_rows": n_rows,
        "created_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "files": {name: hash_file(out_dir / name) for name in ARTIFACT_FILES},
    }
    path = out_dir / "manifest.json"
    path.write_text(json.dumps(manifest, indent=2))
    return path


def subgroup_metrics(X_test: pd.DataFrame, y_test, pred, proba) -> dict:
    out = {}
    for col in ("Geography", "Gender"):
        groups = {}
        for value in sorted(X_test[col].unique().tolist()):
            mask = (X_test[col] == value).to_numpy()
            if mask.sum() == 0:
                continue
            groups[str(value)] = {
                "n": int(mask.sum()),
                "accuracy": round(float(accuracy_score(y_test[mask], pred[mask])), 4),
                "f1": round(float(f1_score(y_test[mask], pred[mask], zero_division=0)), 4),
                "recall": round(float(recall_score(y_test[mask], pred[mask], zero_division=0)), 4),
                "observed_churn_rate": round(float(y_test[mask].mean()), 4),
                "mean_predicted": round(float(proba[mask].mean()), 4),
            }
        out[col] = groups
    return out


def calibration_bins(y_test, proba, bins: int = 10) -> list:
    edges = np.linspace(0, 1, bins + 1)
    out = []
    for i in range(bins):
        if i == bins - 1:
            mask = (proba >= edges[i]) & (proba <= edges[i + 1])
        else:
            mask = (proba >= edges[i]) & (proba < edges[i + 1])
        n = int(mask.sum())
        out.append(
            {
                "lo": round(float(edges[i]), 2),
                "hi": round(float(edges[i + 1]), 2),
                "n": n,
                "mean_predicted": round(float(proba[mask].mean()), 4) if n else 0.0,
                "observed_rate": round(float(y_test[mask].mean()), 4) if n else 0.0,
            }
        )
    return out


def main(argv=None) -> None:
    args = parse_args(argv)
    df, data_source = load_data(args.data, args.allow_synthetic)
    if data_source == "synthetic":
        logger.warning("Training on SYNTHETIC data; artifacts will be marked as such.")
    test_rows = holdout_mask(df)
    X, y = df[FEATURES], df[TARGET]
    X_train, X_test = X[~test_rows], X[test_rows]
    y_train, y_test = y[~test_rows], y[test_rows]
    results = [evaluate(name, X_train, y_train, X_test, y_test) for name in args.models]
    for r in results:
        logger.info(
            "%-20s cv_f1=%.4f cv_auc=%.4f test_f1=%.4f threshold=%.2f",
            r["name"], r["cv_f1"], r["cv_auc"], r["test_f1"], r["threshold"],
        )
    best = max(results, key=lambda r: (r["cv_f1"], r["cv_auc"]))
    model, threshold = best["model"], best["threshold"]
    proba = model.predict_proba(X_test)[:, 1]
    pred = (proba >= threshold).astype(int)
    acc = accuracy_score(y_test, pred)
    f1 = f1_score(y_test, pred)
    logger.info("selected %s: threshold=%.2f accuracy=%.4f f1=%.4f", best["name"], threshold, acc, f1)

    gap = recall_gap(X_test, y_test, pred)
    previous = current_scores(args.current, X_test, y_test)
    if previous is not None:
        if f1 < previous["f1"]:
            logger.warning("New f1=%.4f is below the current model's %.4f on the same rows; nothing written.", f1, previous["f1"])
            return
        if gap > previous["gap"] + MAX_GAP_INCREASE:
            logger.warning("Recall gap between groups grew from %.3f to %.3f; nothing written.", previous["gap"], gap)
            return

    out_dir = args.out
    out_dir.mkdir(parents=True, exist_ok=True)
    joblib.dump(model, out_dir / "churn_model.pkl")

    (out_dir / "columns.json").write_text(
        json.dumps(
            {
                "features": FEATURES,
                "categorical": CATEGORICAL,
                "numeric": NUMERIC,
                "target": TARGET,
                "algorithm": best["name"],
                "threshold": threshold,
                "reference": reference_row(X_train),
                "model_version": args.model_version,
                "data_source": data_source,
                "n_rows": len(df),
            },
            indent=2,
        )
    )
    (out_dir / "metrics.json").write_text(
        json.dumps(
            {
                "algorithm": best["name"],
                "threshold": threshold,
                "accuracy": acc,
                "f1": f1,
                "roc_auc": float(roc_auc_score(y_test, proba)),
                "brier": float(brier_score_loss(y_test, proba)),
                "recall_gap": gap,
                "n_train": len(X_train),
                "n_test": len(X_test),
                "model_version": args.model_version,
                "data_source": data_source,
                "candidates": [{k: v for k, v in r.items() if k != "model"} for r in results],
                "feature_importance": importances(model, X_test, y_test),
                "confusion_matrix": {
                    "labels": ["stayed(0)", "churned(1)"],
                    "matrix": confusion_matrix(y_test, pred).tolist(),
                },
                "subgroup_metrics": subgroup_metrics(X_test, y_test, pred, proba),
                "calibration": calibration_bins(y_test, proba),
            },
            indent=2,
        )
    )
    stats = build_stats(df)
    stats["model_version"] = args.model_version
    stats["data_source"] = data_source
    (out_dir / "stats.json").write_text(json.dumps(stats, indent=2))
    write_manifest(out_dir, model_version=args.model_version, data_source=data_source, n_rows=len(df))
    logger.info("Saved artifacts to %s", out_dir)


def build_stats(df: pd.DataFrame) -> dict:
    def rate(mask) -> dict:
        sub = df[mask]
        return {"rate": round(float(sub[TARGET].mean()), 4) if len(sub) else 0.0, "n": len(sub)}

    def binned(col: str, bins: int, lo: float, hi: float) -> dict:
        edges = np.linspace(lo, hi, bins + 1).tolist()
        labels, rates, ns = [], [], []
        for i in range(bins):
            m = (df[col] >= edges[i]) & (df[col] <= edges[i + 1] if i == bins - 1 else df[col] < edges[i + 1])
            r = rate(m)
            labels.append(f"{edges[i]:.0f}–{edges[i+1]:.0f}")
            rates.append(r["rate"])
            ns.append(r["n"])
        return {"labels": labels, "churn_rate": rates, "n": ns}

    return {
        "n_rows": len(df),
        "overall_churn_rate": round(float(df[TARGET].mean()), 4),
        "by_geography": {g: rate(df["Geography"] == g) for g in sorted(df["Geography"].unique().tolist())},
        "by_gender": {g: rate(df["Gender"] == g) for g in sorted(df["Gender"].unique().tolist())},
        "by_products": {str(k): rate(df["NumOfProducts"] == k) for k in sorted(df["NumOfProducts"].unique().tolist())},
        "age_bins": binned("Age", 8, 18, 92),
        "credit_bins": binned("CreditScore", 8, 350, 850),
        "balance_bins": binned("Balance", 8, 0, float(df["Balance"].max() or 1)),
    }


if __name__ == "__main__":
    main()
