import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import train

FAST = ["logistic_regression", "gradient_boosting"]


def test_training_writes_verified_bundle(tmp_path):
    train.main(["--allow-synthetic", "--out", str(tmp_path), "--data", str(tmp_path / "missing.csv"), "--models", *FAST])
    manifest = json.loads((tmp_path / "manifest.json").read_text())
    for name, digest in manifest["files"].items():
        assert train.hash_file(tmp_path / name) == digest
    metrics = json.loads((tmp_path / "metrics.json").read_text())
    assert {c["name"] for c in metrics["candidates"]} == set(FAST)
    best = max(metrics["candidates"], key=lambda c: (c["cv_f1"], c["cv_auc"]))
    assert metrics["algorithm"] == best["name"]
    assert abs(sum(f["importance"] for f in metrics["feature_importance"]) - 1) < 0.01
    columns = json.loads((tmp_path / "columns.json").read_text())
    assert columns["threshold"] == best["threshold"]
    assert 0 < columns["threshold"] < 1
    assert metrics["brier"] < 0.25
    assert set(columns["reference"]) == set(train.FEATURES)
    assert columns["data_source"] == "synthetic"


def test_data_folder_is_concatenated_and_deduplicated(tmp_path):
    df = train.generate_synthetic(200)
    (tmp_path / "a.csv").write_text(df.to_csv(index=False))
    (tmp_path / "b.csv").write_text(df.to_csv(index=False))
    loaded, source = train.load_data(tmp_path, allow_synthetic=False)
    assert source == "real"
    assert len(loaded) == 200


def test_real_data_required_without_flag(tmp_path):
    try:
        train.load_data(tmp_path / "missing.csv", allow_synthetic=False)
    except SystemExit:
        return
    raise AssertionError("expected SystemExit")


def test_worse_model_does_not_replace_current(tmp_path):
    current, candidate = tmp_path / "current", tmp_path / "candidate"
    data = tmp_path / "missing.csv"
    train.main(["--allow-synthetic", "--out", str(current), "--data", str(data), "--models", *FAST])
    columns = json.loads((current / "columns.json").read_text())
    columns["threshold"] = 0.5
    (current / "columns.json").write_text(json.dumps(columns))
    metrics_before = (current / "metrics.json").read_text()
    train.main(["--allow-synthetic", "--out", str(candidate), "--data", str(data), "--current", str(current), "--models", *FAST])
    assert (current / "metrics.json").read_text() == metrics_before


def test_every_candidate_trains_and_calibrates():
    df = train.generate_synthetic(400)
    X, y = df[train.FEATURES], df[train.TARGET]
    for name in train.CANDIDATES:
        model = train.build_model(name, len(X)).fit(X, y)
        proba = model.predict_proba(X)[:, 1]
        assert ((proba >= 0) & (proba <= 1)).all(), name


def test_files_missing_columns_are_skipped(tmp_path):
    good = train.generate_synthetic(100)
    (tmp_path / "good.csv").write_text(good.to_csv(index=False))
    (tmp_path / "bad.csv").write_text("CreditScore,Age\n600,40\n")
    loaded, _ = train.load_data(tmp_path, allow_synthetic=False)
    assert len(loaded) == 100


def test_holdout_is_stable_when_rows_are_added():
    df = train.generate_synthetic(500, seed=1)
    more = train.generate_synthetic(500, seed=2)
    before = train.holdout_mask(df)
    after = train.holdout_mask(__import__("pandas").concat([df, more], ignore_index=True))
    assert (before == after[:500]).all()
    assert 0.1 < before.mean() < 0.3


def test_recall_gap_measures_spread_between_groups():
    import numpy as np
    import pandas as pd

    X = pd.DataFrame({"Geography": ["France"] * 40 + ["Spain"] * 40, "Gender": ["Male"] * 80})
    y = np.ones(80, dtype=int)
    pred = np.array([1] * 40 + [0] * 20 + [1] * 20)
    assert abs(train.recall_gap(X, y, pred) - 0.5) < 1e-9
