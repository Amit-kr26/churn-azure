import datetime
import hashlib
import hmac
import io
import json
import logging
import math
import os
import re
import tempfile
import threading
import time
import uuid
from collections import defaultdict, deque
from pathlib import Path

import pandas as pd
from fastapi import BackgroundTasks, FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator
from starlette.concurrency import run_in_threadpool

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("churn-api")


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.getenv(name) or default)
    except ValueError:
        return default


DEFAULT_MODEL_DIR = Path(os.getenv("MODEL_DIR", Path(__file__).resolve().parent / "model"))
MODELS_CONTAINER = "models"
POINTER_BLOB = "latest.json"
REFRESH_SECONDS = _int_env("MODEL_REFRESH_SECONDS", 300)
MAX_BATCH_ROWS = _int_env("MAX_BATCH_ROWS", 500)
MAX_BATCH_BYTES = _int_env("MAX_BATCH_BYTES", 1_000_000)
MAX_ROW_ERRORS = 25
HISTORY_LIMIT = 200
RATE_LIMIT_EXEMPT_PATHS = {"/", "/health", "/ready", "/docs", "/redoc", "/openapi.json"}
REQUIRED_COLUMNS = [
    "CreditScore", "Geography", "Gender", "Age", "Tenure", "Balance",
    "NumOfProducts", "HasCrCard", "IsActiveMember", "EstimatedSalary",
]

_model = None
_model_version = "none"
_model_dir = DEFAULT_MODEL_DIR
_reference: dict = {}
_threshold = 0.5
_model_created = ""
_load_lock = threading.Lock()
_rate_buckets: dict[str, deque[float]] = defaultdict(deque)


def _verify_artifact_manifest(model_dir: Path) -> tuple[bool, str]:
    manifest_path = model_dir / "manifest.json"
    if not manifest_path.exists():
        return True, "absent"
    try:
        manifest = json.loads(manifest_path.read_text())
    except ValueError as exc:
        return False, f"unreadable manifest: {exc}"
    files = manifest.get("files", {})
    if not isinstance(files, dict) or not files:
        return False, "manifest lists no files"
    for name, expected in files.items():
        path = model_dir / name
        if not path.exists():
            return False, f"missing artifact: {name}"
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if f"sha256:{digest}" != expected:
            return False, f"checksum mismatch: {name}"
    return True, f"verified {len(files)} artifact(s)"


def _latest_container():
    account = os.getenv("STORAGE_ACCOUNT_NAME", "").strip()
    if not account:
        return None
    from azure.identity import DefaultAzureCredential
    from azure.storage.blob import ContainerClient

    return ContainerClient(
        f"https://{account}.blob.core.windows.net",
        MODELS_CONTAINER,
        credential=DefaultAzureCredential(),
    )


def _is_newer(created: str, known: str) -> bool:
    try:
        return datetime.datetime.fromisoformat(created) > datetime.datetime.fromisoformat(known)
    except ValueError:
        return bool(created) and created != known


def _sync_latest_model(known_created: str = "") -> Path | None:
    try:
        client = _latest_container()
        if client is None:
            return None
        pointer = json.loads(client.download_blob(POINTER_BLOB).readall())
        created, prefix = pointer.get("created_utc") or "", pointer.get("prefix", "")
        if not created or not prefix or (known_created and not _is_newer(created, known_created)):
            return None
        target = Path(tempfile.mkdtemp(prefix="model-"))
        for blob in client.list_blobs(name_starts_with=prefix):
            (target / Path(blob.name).name).write_bytes(client.download_blob(blob.name).readall())
        return target
    except Exception as exc:
        logger.warning("Model sync from blob failed (%s).", type(exc).__name__)
        return None


def _load_artifacts(model_dir: Path) -> bool:
    global _model, _model_version, _model_dir, _reference, _model_created, _threshold
    model_path = model_dir / "churn_model.pkl"
    columns_path = model_dir / "columns.json"
    if not (model_path.exists() and columns_path.exists()):
        logger.warning("No model artifacts in %s.", model_dir)
        return False
    ok, detail = _verify_artifact_manifest(model_dir)
    if not ok:
        logger.error("Artifact integrity check failed: %s", detail)
        return False
    try:
        import joblib

        model = joblib.load(model_path)
        columns = json.loads(columns_path.read_text())
        created = json.loads((model_dir / "manifest.json").read_text()).get("created_utc", "")
    except Exception as exc:
        logger.error("Could not load model: %s", exc)
        return False
    with _load_lock:
        _model = model
        _model_version = columns.get("model_version", "unknown")
        _reference = columns.get("reference", {})
        _threshold = float(columns.get("threshold", 0.5))
        _model_created = created
        _model_dir = model_dir
    logger.info("Loaded %s %s from %s (%s)", columns.get("algorithm", "model"), _model_version, model_dir, detail)
    return True


def refresh_model() -> bool:
    synced = _sync_latest_model(_model_created)
    return synced is not None and _load_artifacts(synced)


def _refresh_loop() -> None:
    while True:
        time.sleep(REFRESH_SECONDS)
        refresh_model()


def _startup() -> None:
    if not _load_artifacts(DEFAULT_MODEL_DIR):
        logger.warning("Bundled model unavailable.")
    refresh_model()
    if os.getenv("STORAGE_ACCOUNT_NAME", "").strip():
        threading.Thread(target=_refresh_loop, daemon=True).start()


_startup()

app = FastAPI(title="Bank Customer Churn Prediction", version="2.0.0")


def _allowed_origins() -> list[str]:
    raw = os.getenv("ALLOWED_ORIGINS", "").strip()
    if raw:
        return [o.strip() for o in raw.split(",") if o.strip()]
    return ["http://localhost:8000", "http://127.0.0.1:8000", "http://localhost:5500"]


def _client_key(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        return forwarded.split(",")[-1].strip()
    return request.client.host if request.client else "unknown"


@app.middleware("http")
async def _rate_limit_middleware(request: Request, call_next):
    if request.url.path not in RATE_LIMIT_EXEMPT_PATHS and request.method != "OPTIONS":
        limit = max(1, _int_env("RATE_LIMIT_PER_MINUTE", 600))
        now = time.monotonic()
        for key in [k for k, b in _rate_buckets.items() if not b or now - b[-1] > 60]:
            del _rate_buckets[key]
        bucket = _rate_buckets[_client_key(request)]
        while bucket and now - bucket[0] > 60:
            bucket.popleft()
        if len(bucket) >= limit:
            return JSONResponse(status_code=429, content={"detail": "rate limit exceeded"})
        bucket.append(now)
    return await call_next(request)


@app.middleware("http")
async def _security_headers_middleware(request: Request, call_next):
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    return response


app.add_middleware(
    CORSMiddleware,
    allow_origins=_allowed_origins(),
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Content-Type", "X-API-Key", "Authorization", "X-Session"],
)


def _json_safe(value):
    if isinstance(value, float) and not math.isfinite(value):
        return repr(value)
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return repr(value)


@app.exception_handler(RequestValidationError)
async def _validation_exception_handler(request: Request, exc: RequestValidationError):
    return JSONResponse(status_code=422, content={"detail": _json_safe(exc.errors())})


def _require_api_key(request: Request) -> None:
    expected = os.getenv("API_KEY", "")
    if expected and not hmac.compare_digest(request.headers.get("X-API-Key", ""), expected):
        raise HTTPException(status_code=401, detail="invalid or missing API key")


_jwks_cache: dict = {"keys": None, "fetched_at": 0.0}
JWKS_CACHE_SECONDS = 3600


def _fetch_jwks(url: str) -> dict:
    import urllib.request

    with urllib.request.urlopen(url, timeout=10) as response:
        return json.loads(response.read().decode("utf-8"))


def _get_jwks_keys(jwks_url: str) -> dict:
    now = time.monotonic()
    if _jwks_cache["keys"] is not None and now - _jwks_cache["fetched_at"] < JWKS_CACHE_SECONDS:
        return _jwks_cache["keys"]
    try:
        doc = _fetch_jwks(jwks_url)
    except Exception as exc:
        logger.warning("Could not fetch JWKS (%s).", type(exc).__name__)
        raise HTTPException(status_code=503, detail="authentication temporarily unavailable") from exc
    keys = {k["kid"]: k for k in doc.get("keys", []) if "kid" in k}
    _jwks_cache.update({"keys": keys, "fetched_at": now})
    return keys


def _require_jwt(request: Request) -> None:
    issuer = os.getenv("JWT_ISSUER", "").strip()
    audience = os.getenv("JWT_AUDIENCE", "").strip()
    jwks_url = os.getenv("JWT_JWKS_URL", "").strip()
    if not audience:
        return
    if not issuer or not jwks_url:
        raise HTTPException(status_code=503, detail="authentication misconfigured")
    header = request.headers.get("Authorization", "")
    if not header.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="missing bearer token")
    token = header[7:].strip()
    try:
        import jwt as pyjwt

        keys = _get_jwks_keys(jwks_url)
        kid = pyjwt.get_unverified_header(token).get("kid")
        key_data = keys.get(kid)
        if key_data is None:
            _jwks_cache["fetched_at"] = 0.0
            key_data = _get_jwks_keys(jwks_url).get(kid)
        if key_data is None:
            raise HTTPException(status_code=401, detail="unknown signing key")
        public_key = pyjwt.algorithms.RSAAlgorithm.from_jwk(json.dumps(key_data))
        pyjwt.decode(
            token,
            public_key,
            algorithms=["RS256"],
            audience=audience,
            issuer=issuer,
            options={"require": ["exp", "iss", "aud"]},
        )
    except HTTPException:
        raise
    except Exception as exc:
        logger.info("JWT rejected (%s).", type(exc).__name__)
        raise HTTPException(status_code=401, detail="invalid token") from exc


def _require_auth(request: Request) -> None:
    _require_api_key(request)
    _require_jwt(request)


_memory_history: deque[dict] = deque(maxlen=HISTORY_LIMIT)
_cosmos_container = None


def _cosmos():
    global _cosmos_container
    endpoint = os.getenv("COSMOS_ENDPOINT", "").strip()
    if not endpoint:
        return None
    if _cosmos_container is None:
        from azure.cosmos import CosmosClient
        from azure.identity import DefaultAzureCredential

        database = os.getenv("COSMOS_DATABASE", "churn")
        container = os.getenv("COSMOS_CONTAINER", "predictions")
        _cosmos_container = (
            CosmosClient(endpoint, credential=DefaultAzureCredential())
            .get_database_client(database)
            .get_container_client(container)
        )
    return _cosmos_container


SESSION_PATTERN = re.compile(r"^[0-9a-f]{32}$")


def _session_id(request: Request) -> str:
    value = request.headers.get("X-Session", "")
    return value if SESSION_PATTERN.match(value) else ""


def _record_prediction(record: dict) -> None:
    now = datetime.datetime.now(datetime.timezone.utc)
    item = {
        "id": uuid.uuid4().hex,
        "day": f"{now:%Y-%m-%d}",
        "ts": now.isoformat(),
        "model_version": _model_version,
        **record,
    }
    try:
        container = _cosmos()
        if container is None:
            _memory_history.append(item)
        else:
            container.create_item(item)
    except Exception as exc:
        logger.warning("Could not store prediction (%s).", type(exc).__name__)


def _read_history(session: str, limit: int) -> list[dict]:
    if not session:
        return []
    container = _cosmos()
    if container is None:
        return [i for i in reversed(_memory_history) if i.get("session") == session][:limit]
    rows = container.query_items(
        "SELECT TOP @n c.ts, c.endpoint, c.churn, c.probability, c.features, "
        "c.n_scored, c.n_churn, c.model_version FROM c WHERE c.session = @s ORDER BY c.ts DESC",
        parameters=[{"name": "@n", "value": limit}, {"name": "@s", "value": session}],
        enable_cross_partition_query=True,
    )
    return list(rows)


ALLOWED_GEOGRAPHY = {"France", "Spain", "Germany"}
ALLOWED_GENDER = {"Male", "Female"}


class ChurnFeatures(BaseModel):
    model_config = ConfigDict(extra="forbid")

    CreditScore: int = Field(..., ge=300, le=900)
    Geography: str
    Gender: str
    Age: int = Field(..., ge=18, le=100)
    Tenure: int = Field(..., ge=0, le=10)
    Balance: float = Field(..., ge=0)
    NumOfProducts: int = Field(..., ge=1, le=4)
    HasCrCard: int = Field(..., ge=0, le=1)
    IsActiveMember: int = Field(..., ge=0, le=1)
    EstimatedSalary: float = Field(..., ge=0)

    @field_validator("Geography")
    @classmethod
    def _check_geo(cls, v: str) -> str:
        if v not in ALLOWED_GEOGRAPHY:
            raise ValueError(f"Geography must be one of {sorted(ALLOWED_GEOGRAPHY)}")
        return v

    @field_validator("Gender")
    @classmethod
    def _check_gender(cls, v: str) -> str:
        if v not in ALLOWED_GENDER:
            raise ValueError(f"Gender must be one of {sorted(ALLOWED_GENDER)}")
        return v

    @field_validator("Balance", "EstimatedSalary")
    @classmethod
    def _check_finite(cls, v: float) -> float:
        if not math.isfinite(v):
            raise ValueError("value must be finite")
        return v


def _require_model() -> None:
    if _model is None:
        raise HTTPException(status_code=503, detail="no trained model loaded")


def _predict_records(records: list[ChurnFeatures]) -> list[float]:
    _require_model()
    frame = pd.DataFrame([r.model_dump() for r in records])
    return [float(p) for p in _model.predict_proba(frame)[:, 1]]


async def _read_capped_body(request: Request) -> bytes:
    try:
        declared = int(request.headers.get("content-length") or 0)
    except ValueError:
        declared = 0
    too_large = HTTPException(
        status_code=413, detail=f"request body too large; limit is {MAX_BATCH_BYTES} bytes"
    )
    if declared > MAX_BATCH_BYTES:
        raise too_large
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > MAX_BATCH_BYTES:
            raise too_large
    return bytes(body)


def _validate_batch_rows(df: pd.DataFrame) -> tuple[list[ChurnFeatures], list[dict]]:
    records: list[ChurnFeatures] = []
    row_errors: list[dict] = []
    for i, row in enumerate(df[REQUIRED_COLUMNS].to_dict("records")):
        values = {k: (None if pd.isna(v) else v.item() if hasattr(v, "item") else v) for k, v in row.items()}
        try:
            records.append(ChurnFeatures.model_validate(values))
        except ValidationError as exc:
            row_errors.append(
                {
                    "row": i,
                    "errors": [
                        {
                            "loc": list(e.get("loc", [])),
                            "msg": e.get("msg"),
                            "type": e.get("type"),
                            "input": _json_safe(e.get("input")),
                        }
                        for e in exc.errors(include_url=False)
                    ],
                }
            )
            if len(row_errors) >= MAX_ROW_ERRORS:
                break
    return records, row_errors


@app.get("/")
def root() -> dict:
    return {
        "service": "bank-churn-prediction",
        "docs": "/docs",
        "model_version": _model_version,
    }


@app.get("/health")
def health() -> dict:
    loaded = _model is not None
    return {
        "status": "ok" if loaded else "degraded",
        "model_loaded": loaded,
        "model_version": _model_version,
        "prediction_source": "model" if loaded else "none",
    }


@app.get("/ready")
def ready():
    if _model is None:
        return JSONResponse(
            status_code=503,
            content={"ready": False, "model_loaded": False, "detail": "no trained model loaded"},
        )
    return {"ready": True, "model_loaded": True, "model_version": _model_version}


@app.post("/predict")
def predict(features: ChurnFeatures, request: Request, background: BackgroundTasks) -> dict:
    _require_auth(request)
    probability = _predict_records([features])[0]
    churn = int(probability >= _threshold)
    background.add_task(
        _record_prediction,
        {"session": _session_id(request), "endpoint": "predict", "features": features.model_dump(), "churn": churn, "probability": probability},
    )
    return {
        "churn": churn,
        "probability": probability,
        "model_version": _model_version,
        "prediction_source": "model",
        "threshold": _threshold,
    }


@app.get("/history")
def history(request: Request, limit: int = 50) -> dict:
    _require_auth(request)
    try:
        items = _read_history(_session_id(request), max(1, min(limit, HISTORY_LIMIT)))
    except Exception as exc:
        logger.warning("History read failed (%s).", type(exc).__name__)
        raise HTTPException(status_code=503, detail="history unavailable") from exc
    return {"items": items}


@app.get("/metrics")
def metrics(request: Request) -> dict:
    _require_auth(request)
    path = _model_dir / "metrics.json"
    if not path.exists():
        raise HTTPException(status_code=404, detail="metrics.json not found; train first")
    body = json.loads(path.read_text())
    body["model_loaded"] = _model is not None
    return body


@app.get("/stats")
def stats(request: Request) -> dict:
    _require_auth(request)
    path = _model_dir / "stats.json"
    if not path.exists():
        raise HTTPException(status_code=404, detail="stats.json not found; train first")
    return json.loads(path.read_text())


@app.post("/explain")
def explain(features: ChurnFeatures, request: Request) -> dict:
    _require_auth(request)
    _require_model()
    row = features.model_dump()
    reference = {**row, **_reference}
    frames = [row] + [{**row, name: reference[name]} for name in row]
    probabilities = _model.predict_proba(pd.DataFrame(frames))[:, 1]
    probability = float(probabilities[0])
    contrib = sorted(
        (
            {"feature": name, "contribution": round(probability - float(p), 4)}
            for name, p in zip(row, probabilities[1:], strict=True)
        ),
        key=lambda d: abs(d["contribution"]),
        reverse=True,
    )
    return {
        "churn": int(probability >= _threshold),
        "probability": probability,
        "model_version": _model_version,
        "prediction_source": "model",
        "threshold": _threshold,
        "toward_churn": [d for d in contrib if d["contribution"] > 0][:5],
        "toward_stay": [d for d in contrib if d["contribution"] < 0][:5],
    }


@app.get("/feature_importance")
def feature_importance(request: Request, top_n: int = 10) -> dict:
    _require_auth(request)
    path = _model_dir / "metrics.json"
    if not path.exists():
        raise HTTPException(status_code=404, detail="metrics.json not found; train first")
    ranked = json.loads(path.read_text()).get("feature_importance", [])
    return {"model_version": _model_version, "features": ranked[: max(1, top_n)]}


@app.post("/batch_predict")
async def batch_predict(request: Request, background: BackgroundTasks) -> dict:
    await run_in_threadpool(_require_auth, request)
    raw = await _read_capped_body(request)
    if not raw:
        raise HTTPException(status_code=422, detail="empty request body; send CSV bytes")
    try:
        df = pd.read_csv(io.BytesIO(raw), nrows=MAX_BATCH_ROWS + 1)
    except Exception as exc:
        raise HTTPException(status_code=400, detail="could not parse CSV") from exc
    missing = [c for c in REQUIRED_COLUMNS if c not in df.columns]
    if missing:
        raise HTTPException(status_code=422, detail=f"missing columns: {missing}")
    if len(df) == 0:
        raise HTTPException(status_code=422, detail="empty CSV")
    if len(df) > MAX_BATCH_ROWS:
        raise HTTPException(
            status_code=413,
            detail=f"CSV has more than {MAX_BATCH_ROWS} data rows; split the file and retry",
        )
    records, row_errors = _validate_batch_rows(df)
    if row_errors:
        raise HTTPException(
            status_code=422,
            detail={
                "message": f"{len(row_errors)} invalid row(s) rejected (showing up to {MAX_ROW_ERRORS})",
                "row_errors": row_errors,
            },
        )
    proba = await run_in_threadpool(_predict_records, records)
    rows = [
        {"row": i + 1, "churn": int(p >= _threshold), "probability": round(p, 4)}
        for i, p in enumerate(proba)
    ]
    n_churn = sum(r["churn"] for r in rows)
    background.add_task(
        _record_prediction,
        {"session": _session_id(request), "endpoint": "batch_predict", "n_scored": len(rows), "n_churn": n_churn},
    )
    return {
        "model_version": _model_version,
        "prediction_source": "model",
        "threshold": _threshold,
        "n_scored": len(rows),
        "n_churn": n_churn,
        "rows": rows,
    }
