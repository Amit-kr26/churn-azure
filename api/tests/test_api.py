import hashlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import app as app_module
from app import app
from fastapi.testclient import TestClient

client = TestClient(app)

SAMPLE = {
    "CreditScore": 650,
    "Geography": "France",
    "Gender": "Male",
    "Age": 35,
    "Tenure": 5,
    "Balance": 50000.0,
    "NumOfProducts": 1,
    "HasCrCard": 1,
    "IsActiveMember": 1,
    "EstimatedSalary": 80000.0,
}


def test_health():
    r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["model_loaded"] is True
    assert "model_version" in body


def test_ready():
    r = client.get("/ready")
    assert r.status_code == 200
    assert r.json()["ready"] is True


def test_root():
    r = client.get("/")
    assert r.status_code == 200
    assert r.json()["service"] == "bank-churn-prediction"


def test_predict_stayed():
    r = client.post("/predict", json=SAMPLE)
    assert r.status_code == 200
    body = r.json()
    assert set(body) == {"churn", "probability", "model_version", "prediction_source", "threshold"}
    assert body["churn"] in (0, 1)
    assert 0.0 <= body["probability"] <= 1.0
    assert body["prediction_source"] == "model"


def test_predict_likely_churn():
    payload = dict(SAMPLE, CreditScore=400, Age=70, IsActiveMember=0)
    r = client.post("/predict", json=payload)
    assert r.status_code == 200
    assert r.json()["churn"] in (0, 1)


def test_predict_bad_geography():
    r = client.post("/predict", json=dict(SAMPLE, Geography="Mars"))
    assert r.status_code == 422


def test_predict_rejects_extra_fields():
    r = client.post("/predict", json=dict(SAMPLE, CustomerId="123", Exited=1))
    assert r.status_code == 422


def test_predict_rejects_infinite_balance():
    raw = (
        '{"CreditScore":650,"Geography":"France","Gender":"Male","Age":35,'
        '"Tenure":5,"Balance":Infinity,"NumOfProducts":1,"HasCrCard":1,'
        '"IsActiveMember":1,"EstimatedSalary":80000}'
    )
    r = client.post("/predict", content=raw, headers={"Content-Type": "application/json"})
    assert r.status_code == 422


BATCH_CSV = (
    "CreditScore,Geography,Gender,Age,Tenure,Balance,NumOfProducts,HasCrCard,IsActiveMember,EstimatedSalary\n"
    "650,France,Female,35,5,0,2,1,1,72000\n"
    "500,Germany,Male,60,2,136500,1,1,0,92000\n"
)


def test_metrics():
    r = client.get("/metrics")
    assert r.status_code == 200
    body = r.json()
    assert set(body) >= {"accuracy", "f1", "n_train", "n_test", "model_version"}


def test_feature_importance():
    r = client.get("/feature_importance?top_n=5")
    assert r.status_code == 200
    feats = r.json()["features"]
    assert 1 <= len(feats) <= 5
    assert all("feature" in f and "importance" in f for f in feats)


def test_batch_predict():
    r = client.post("/batch_predict", content=BATCH_CSV.encode(), headers={"Content-Type": "text/csv"})
    assert r.status_code == 200
    body = r.json()
    assert body["n_scored"] == 2
    assert body["prediction_source"] == "model"
    assert len(body["rows"]) == 2
    assert set(body["rows"][0]) == {"row", "churn", "probability"}
    assert body["rows"][0]["row"] == 1


def test_batch_predict_missing_column():
    bad = "CreditScore,Age\n650,35\n"
    r = client.post("/batch_predict", content=bad.encode(), headers={"Content-Type": "text/csv"})
    assert r.status_code == 422


def test_batch_predict_rejects_oversize():
    header = "CreditScore,Geography,Gender,Age,Tenure,Balance,NumOfProducts,HasCrCard,IsActiveMember,EstimatedSalary"
    row = "650,France,Male,35,5,0,1,1,1,80000"
    big = header + "\n" + "\n".join([row] * 501)
    r = client.post("/batch_predict", content=big.encode(), headers={"Content-Type": "text/csv"})
    assert r.status_code == 413


def test_batch_predict_rejects_invalid_rows():
    header = "CreditScore,Geography,Gender,Age,Tenure,Balance,NumOfProducts,HasCrCard,IsActiveMember,EstimatedSalary"
    bad = header + "\n650,Mars,Male,35,5,0,1,1,1,80000\n650,France,Male,1000,5,0,1,1,1,80000\n"
    r = client.post("/batch_predict", content=bad.encode(), headers={"Content-Type": "text/csv"})
    assert r.status_code == 422
    body = r.json()
    assert isinstance(body["detail"], dict)
    assert len(body["detail"]["row_errors"]) == 2


def test_stats():
    r = client.get("/stats")
    assert r.status_code == 200
    body = r.json()
    assert set(body) >= {"overall_churn_rate", "by_geography", "age_bins", "n_rows"}
    assert body["n_rows"] >= 1000


def test_explain():
    r = client.post("/explain", json=SAMPLE)
    assert r.status_code == 200
    body = r.json()
    assert set(body) >= {"churn", "probability", "toward_churn", "toward_stay"}
    assert len(body["toward_churn"]) >= 1 and len(body["toward_stay"]) >= 1


def test_degraded_without_model(monkeypatch):
    monkeypatch.setattr(app_module, "_model", None)
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "degraded"
    assert r.json()["model_loaded"] is False
    r = client.get("/ready")
    assert r.status_code == 503
    r = client.post("/predict", json=SAMPLE)
    assert r.status_code == 503


def test_cors_does_not_reflect_unknown_origin():
    r = client.options(
        "/predict",
        headers={
            "Origin": "https://evil.example",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type",
        },
    )
    assert "access-control-allow-origin" not in r.headers


def test_api_key(monkeypatch):
    monkeypatch.setenv("API_KEY", "secret")
    r = client.post("/predict", json=SAMPLE)
    assert r.status_code == 401
    r = client.post("/predict", json=SAMPLE, headers={"X-API-Key": "secret"})
    assert r.status_code == 200


def test_rate_limit(monkeypatch):
    monkeypatch.setenv("RATE_LIMIT_PER_MINUTE", "2")
    app_module._rate_buckets.clear()
    try:
        assert client.get("/metrics").status_code == 200
        assert client.get("/metrics").status_code == 200
        assert client.get("/metrics").status_code == 429
    finally:
        app_module._rate_buckets.clear()


def test_security_headers():
    r = client.get("/health")
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["x-frame-options"] == "DENY"


def test_manifest_verification(tmp_path):
    (tmp_path / "a.bin").write_bytes(b"data")
    manifest = {"files": {"a.bin": "sha256:" + hashlib.sha256(b"data").hexdigest()}}
    (tmp_path / "manifest.json").write_text(json.dumps(manifest))
    ok, _ = app_module._verify_artifact_manifest(tmp_path)
    assert ok is True
    (tmp_path / "a.bin").write_bytes(b"tampered")
    ok, reason = app_module._verify_artifact_manifest(tmp_path)
    assert ok is False
    assert "mismatch" in reason


def test_manifest_absent(tmp_path):
    ok, reason = app_module._verify_artifact_manifest(tmp_path)
    assert ok is True
    assert reason == "absent"


def _make_rsa_keypair():
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    numbers = key.public_key().public_numbers()
    import base64

    def b64u(n, length):
        return base64.urlsafe_b64encode(n.to_bytes(length, "big")).rstrip(b"=").decode()

    jwk = {"kty": "RSA", "kid": "test-key", "n": b64u(numbers.n, 256), "e": b64u(numbers.e, 3)}
    return private_pem, jwk


def _jwt_env(monkeypatch):
    monkeypatch.setenv("JWT_ISSUER", "https://login.example/tenant")
    monkeypatch.setenv("JWT_AUDIENCE", "api://churn-test")
    monkeypatch.setenv("JWT_JWKS_URL", "https://login.example/keys")
    app_module._jwks_cache.update({"keys": None, "fetched_at": 0.0})


def _jwt_token(private_pem, **claims):
    import datetime

    import jwt as pyjwt

    now = datetime.datetime.now(datetime.timezone.utc)
    payload = {
        "iss": "https://login.example/tenant",
        "aud": "api://churn-test",
        "exp": now + datetime.timedelta(minutes=5),
        "iat": now,
        **claims,
    }
    return pyjwt.encode(payload, private_pem, algorithm="RS256", headers={"kid": "test-key"})


def test_jwt_valid(monkeypatch):
    private_pem, jwk = _make_rsa_keypair()
    _jwt_env(monkeypatch)
    monkeypatch.setattr(app_module, "_fetch_jwks", lambda url: {"keys": [jwk]})
    token = _jwt_token(private_pem)
    r = client.post("/predict", json=SAMPLE, headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200
    assert r.json()["prediction_source"] == "model"


def test_jwt_missing_token(monkeypatch):
    _jwt_env(monkeypatch)
    r = client.post("/predict", json=SAMPLE)
    assert r.status_code == 401


def test_jwt_wrong_audience(monkeypatch):
    private_pem, jwk = _make_rsa_keypair()
    _jwt_env(monkeypatch)
    monkeypatch.setattr(app_module, "_fetch_jwks", lambda url: {"keys": [jwk]})
    token = _jwt_token(private_pem, aud="api://someone-else")
    r = client.post("/predict", json=SAMPLE, headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 401


def test_jwt_expired(monkeypatch):
    import datetime

    private_pem, jwk = _make_rsa_keypair()
    _jwt_env(monkeypatch)
    monkeypatch.setattr(app_module, "_fetch_jwks", lambda url: {"keys": [jwk]})
    now = datetime.datetime.now(datetime.timezone.utc)
    token = _jwt_token(private_pem, exp=now - datetime.timedelta(minutes=1))
    r = client.post("/predict", json=SAMPLE, headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 401


def test_jwt_misconfigured(monkeypatch):
    monkeypatch.setenv("JWT_AUDIENCE", "api://churn-test")
    monkeypatch.delenv("JWT_ISSUER", raising=False)
    monkeypatch.delenv("JWT_JWKS_URL", raising=False)
    r = client.post("/predict", json=SAMPLE)
    assert r.status_code == 503


class _FakeCosmos:
    def __init__(self, fail=False):
        self.items = []
        self.fail = fail

    def create_item(self, item):
        if self.fail:
            raise ConnectionError("cosmos down")
        self.items.append(item)


SESSION_A = "a" * 32
SESSION_B = "b" * 32


def test_history_in_memory():
    app_module._memory_history.clear()
    assert client.post("/predict", json=SAMPLE, headers={"X-Session": SESSION_A}).status_code == 200
    items = client.get("/history", headers={"X-Session": SESSION_A}).json()["items"]
    assert len(items) == 1
    assert items[0]["endpoint"] == "predict"
    assert items[0]["features"]["Geography"] == "France"


def test_history_is_private_to_each_session():
    app_module._memory_history.clear()
    client.post("/predict", json=SAMPLE, headers={"X-Session": SESSION_A})
    assert client.get("/history", headers={"X-Session": SESSION_B}).json()["items"] == []
    assert client.get("/history").json()["items"] == []
    assert client.get("/history", headers={"X-Session": "not-a-session"}).json()["items"] == []


def test_batch_history_stores_summary_only():
    app_module._memory_history.clear()
    client.post("/batch_predict", content=BATCH_CSV.encode(), headers={"Content-Type": "text/csv", "X-Session": SESSION_A})
    item = client.get("/history", headers={"X-Session": SESSION_A}).json()["items"][0]
    assert item["n_scored"] == 2
    assert "rows" not in item


def test_cosmos_write(monkeypatch):
    fake = _FakeCosmos()
    monkeypatch.setattr(app_module, "_cosmos", lambda: fake)
    assert client.post("/predict", json=SAMPLE).status_code == 200
    assert fake.items[0]["day"] == fake.items[0]["ts"][:10]


def test_storage_failure_never_fails_request(monkeypatch):
    monkeypatch.setattr(app_module, "_cosmos", lambda: _FakeCosmos(fail=True))
    assert client.post("/predict", json=SAMPLE).status_code == 200


def test_cors_allows_authorization_header():
    r = client.options(
        "/predict",
        headers={
            "Origin": "http://localhost:8000",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "authorization",
        },
    )
    assert r.status_code == 200


def test_explain_flags_risk_drivers():
    risky = dict(SAMPLE, Age=62, IsActiveMember=0, Geography="Germany", NumOfProducts=3)
    body = client.post("/explain", json=risky).json()
    assert body["toward_churn"]
    assert all(d["contribution"] > 0 for d in body["toward_churn"])
    assert all(d["contribution"] < 0 for d in body["toward_stay"])
    assert {d["feature"] for d in body["toward_churn"]} <= set(SAMPLE)


def test_hot_reload_picks_up_newer_model(tmp_path, monkeypatch):
    import shutil

    shutil.copytree(app_module.DEFAULT_MODEL_DIR, tmp_path / "latest")
    new_dir = tmp_path / "latest"
    manifest = json.loads((new_dir / "manifest.json").read_text())
    manifest["created_utc"] = "2999-01-01T00:00:00+00:00"
    (new_dir / "manifest.json").write_text(json.dumps(manifest))
    seen = []

    def fake_sync(known_created=""):
        seen.append(known_created)
        return new_dir if known_created != manifest["created_utc"] else None

    monkeypatch.setattr(app_module, "_sync_latest_model", fake_sync)
    try:
        assert app_module.refresh_model() is True
        assert app_module._model_created == manifest["created_utc"]
        assert app_module._model_dir == new_dir
        assert app_module.refresh_model() is False
        assert client.post("/predict", json=SAMPLE).status_code == 200
    finally:
        app_module._load_artifacts(app_module.DEFAULT_MODEL_DIR)


def test_tampered_model_is_rejected(tmp_path):
    import shutil

    shutil.copytree(app_module.DEFAULT_MODEL_DIR, tmp_path / "m")
    (tmp_path / "m" / "churn_model.pkl").write_bytes(b"tampered")
    before = app_module._model_dir
    assert app_module._load_artifacts(tmp_path / "m") is False
    assert app_module._model_dir == before


def test_decision_uses_model_threshold(monkeypatch):
    monkeypatch.setattr(app_module, "_threshold", 0.0)
    assert client.post("/predict", json=SAMPLE).json()["churn"] == 1
    monkeypatch.setattr(app_module, "_threshold", 1.1)
    assert client.post("/predict", json=SAMPLE).json()["churn"] == 0
    assert client.post("/predict", json=SAMPLE).json()["threshold"] == 1.1


def test_only_strictly_newer_bundles_are_loaded():
    assert app_module._is_newer("2026-10-19T00:00:00+00:00", "2026-10-18T00:00:00+00:00")
    assert not app_module._is_newer("2026-10-17T00:00:00+00:00", "2026-10-18T00:00:00+00:00")
    assert not app_module._is_newer("2026-10-18T00:00:00+00:00", "2026-10-18T00:00:00+00:00")


def test_rate_limit_response_carries_cors_headers(monkeypatch):
    monkeypatch.setenv("RATE_LIMIT_PER_MINUTE", "1")
    app_module._rate_buckets.clear()
    origin = app_module._allowed_origins()[0]
    try:
        client.get("/metrics", headers={"Origin": origin})
        limited = client.get("/metrics", headers={"Origin": origin})
        assert limited.status_code == 429
        assert limited.headers["access-control-allow-origin"] == origin
    finally:
        app_module._rate_buckets.clear()


def test_rate_limit_ignores_spoofed_leading_forwarded_for(monkeypatch):
    monkeypatch.setenv("RATE_LIMIT_PER_MINUTE", "1")
    app_module._rate_buckets.clear()
    try:
        assert client.get("/metrics", headers={"X-Forwarded-For": "1.1.1.1, 9.9.9.9"}).status_code == 200
        assert client.get("/metrics", headers={"X-Forwarded-For": "2.2.2.2, 9.9.9.9"}).status_code == 429
    finally:
        app_module._rate_buckets.clear()
