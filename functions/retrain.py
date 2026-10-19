import csv
import datetime
import json
import logging
import uuid

logger = logging.getLogger("churn-retrain")

RAW_FILE_SYSTEM = "raw"
MODELS_FILE_SYSTEM = "models"
BASELINE_PATH = "retrain_baseline.json"
PENDING_PATH = "retrain_pending.json"
POINTER_PATH = "latest.json"
VERSIONS_PREFIX = "versions/"
DECISION_PREFIX = "retrain_decision-"
LOCK_PATH = "retrain.lock"
LOCK_SECONDS = 60
FAILURE_BACKOFF = datetime.timedelta(hours=6)
KEEP_VERSIONS = 5
REQUIRED_COLUMNS = {
    "Exited", "CustomerId", "CreditScore", "Geography", "Gender", "Age", "Tenure",
    "Balance", "NumOfProducts", "HasCrCard", "IsActiveMember", "EstimatedSalary",
}
NOT_FOUND = {"FileNotFoundError", "ResourceNotFoundError"}


def utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def decide_retrain(new_rows: int, threshold: int) -> tuple[bool, str]:
    should = new_rows >= threshold
    return should, f"new_rows({new_rows}) {'>=' if should else '<'} threshold({threshold})"


def iter_lines(chunks):
    buffer = b""
    first = True
    for chunk in chunks:
        buffer += chunk
        *lines, buffer = buffer.split(b"\n")
        for line in lines:
            text = (line + b"\n").decode("utf-8-sig" if first else "utf-8")
            first = False
            yield text
    if buffer:
        yield buffer.decode("utf-8-sig" if first else "utf-8")


def count_csv(lines) -> tuple[bool, int]:
    reader = csv.reader(lines)
    header = next(reader, None)
    if header is None or not REQUIRED_COLUMNS <= set(header):
        return False, 0
    return True, sum(1 for row in reader if any(field.strip() for field in row))


def count_file(fs_client, path: str) -> tuple[bool, int]:
    downloader = fs_client.get_file_client(path).download_file()
    return count_csv(iter_lines(downloader.chunks()))


def list_csv_files(fs_client) -> list[dict]:
    found = []
    for props in fs_client.get_paths(recursive=True):
        name = getattr(props, "name", "")
        if getattr(props, "is_directory", False) or not name.lower().endswith(".csv"):
            continue
        found.append({"path": name, "etag": getattr(props, "etag", None)})
    return found


def refresh_file_state(raw_fs, listed: list[dict], previous: dict, on_progress=None) -> dict:
    state = {}
    for entry in listed:
        prev = previous.get(entry["path"])
        if prev and prev.get("etag") == entry["etag"] and isinstance(prev.get("rows"), int):
            state[entry["path"]] = prev
            continue
        valid, rows = count_file(raw_fs, entry["path"])
        state[entry["path"]] = {
            "etag": entry["etag"],
            "rows": rows,
            "valid": valid,
            "trained_rows": prev.get("trained_rows", 0) if prev else 0,
        }
        if on_progress:
            on_progress()
    return state


def untrained_rows(state: dict) -> int:
    return sum(max(0, f["rows"] - f.get("trained_rows", 0)) for f in state.values() if f["valid"])


def read_json_file(fs_client, path: str):
    try:
        data = fs_client.get_file_client(path).download_file().readall()
    except Exception as exc:
        if type(exc).__name__ in NOT_FOUND:
            return None
        raise
    try:
        return json.loads(data.decode("utf-8"))
    except ValueError:
        logger.warning("Could not parse %s; treating as absent.", path)
        return None


def write_json_file(fs_client, path: str, payload: dict) -> None:
    fs_client.get_file_client(path).upload_data(json.dumps(payload, indent=2).encode("utf-8"), overwrite=True)


def delete_file(fs_client, path: str) -> None:
    try:
        fs_client.delete_file(path)
    except Exception as exc:
        if type(exc).__name__ not in NOT_FOUND:
            raise


def acquire_lock(fs_client):
    file_client = fs_client.get_file_client(LOCK_PATH)
    try:
        file_client.get_file_properties()
    except Exception as exc:
        if type(exc).__name__ not in NOT_FOUND:
            raise
        try:
            file_client.create_file()
        except Exception:
            logger.info("Lock file was created by another check.")
    try:
        return file_client.acquire_lease(lease_duration=LOCK_SECONDS)
    except Exception:
        return None


def release_lock(lease) -> None:
    try:
        lease.release()
    except Exception as exc:
        logger.warning("Could not release the retrain lock (%s); it expires on its own.", type(exc).__name__)


def new_decision(storage_account: str, threshold: int, checked_at: datetime.datetime) -> dict:
    return {
        "decision_id": uuid.uuid4().hex[:12],
        "checked_at_utc": checked_at.isoformat(),
        "storage_account": storage_account,
        "status": "ok",
        "new_rows": 0,
        "threshold": threshold,
        "should_retrain": False,
        "reason": "",
        "files_seen": 0,
        "invalid_files": [],
        "aml_trigger": {"attempted": False, "triggered": False, "detail": ""},
    }


def resolve_pending(models_fs, state: dict, meta: dict, job_status, settings: dict, now: datetime.datetime) -> str | None:
    pending = read_json_file(models_fs, PENDING_PATH)
    if not pending:
        return None
    status = job_status(settings, pending["job"])
    if status == "running":
        return f"job {pending['job']} is still running"
    delete_file(models_fs, PENDING_PATH)
    if status == "failed":
        logger.error("retrain job failed: %s", pending["job"])
        meta["failed_at"] = now.isoformat()
        return None
    for path, rows in pending.get("trained", {}).items():
        if path in state:
            state[path]["trained_rows"] = min(rows, state[path]["rows"])
    manifest = read_json_file(models_fs, f"{pending['prefix']}manifest.json")
    if manifest:
        write_json_file(
            models_fs,
            POINTER_PATH,
            {"prefix": pending["prefix"], "created_utc": manifest.get("created_utc"), "model_version": manifest.get("model_version")},
        )
        logger.info("retrain job %s published %s", pending["job"], pending["prefix"])
    else:
        logger.info("retrain job %s finished without publishing; the current model stays", pending["job"])
    meta.pop("failed_at", None)
    return None


def run_retrain_check(
    service_client,
    *,
    storage_account: str,
    threshold: int,
    aml_enabled: bool,
    aml_settings: dict | None = None,
    job_status=None,
    now: datetime.datetime | None = None,
) -> dict:
    checked_at = now or utcnow()
    decision = new_decision(storage_account, threshold, checked_at)
    try:
        raw_fs = service_client.get_file_system_client(RAW_FILE_SYSTEM)
        models_fs = service_client.get_file_system_client(MODELS_FILE_SYSTEM)
        lease = acquire_lock(models_fs)
        if lease is None:
            decision.update(status="busy", reason="another check is already running")
            return decision
        try:
            return _check(decision, raw_fs, models_fs, lease, threshold, aml_enabled, aml_settings or {}, job_status or aml_job_status, checked_at)
        finally:
            release_lock(lease)
    except Exception as exc:
        logger.warning("Retrain check failed (%s: %s).", type(exc).__name__, exc)
        decision.update(status="error", reason=f"check failed: {type(exc).__name__}: {exc}")
    return decision


def _check(decision, raw_fs, models_fs, lease, threshold, aml_enabled, settings, job_status, checked_at) -> dict:
    baseline = read_json_file(models_fs, BASELINE_PATH) or {}
    meta = {k: v for k, v in baseline.items() if k != "files"}
    state = baseline.get("files", {})

    waiting = resolve_pending(models_fs, state, meta, job_status, settings, checked_at) if aml_enabled else None
    state = refresh_file_state(raw_fs, list_csv_files(raw_fs), state, on_progress=lease.renew)
    new_rows = untrained_rows(state)
    should, reason = decide_retrain(new_rows, threshold)
    decision.update(
        new_rows=new_rows,
        should_retrain=should,
        reason=reason,
        files_seen=len(state),
        invalid_files=sorted(p for p, f in state.items() if not f["valid"]),
    )
    if decision["invalid_files"]:
        logger.warning("files missing required columns are ignored: %s", decision["invalid_files"])

    failed_at = meta.get("failed_at")
    backing_off = bool(failed_at) and checked_at - datetime.datetime.fromisoformat(failed_at) < FAILURE_BACKOFF
    if waiting:
        decision.update(status="waiting", reason=waiting)
    elif should and aml_enabled and backing_off:
        decision.update(status="backoff", reason="the last retrain job failed recently; trying again later")
    elif should and aml_enabled:
        pointer = read_json_file(models_fs, POINTER_PATH)
        prefix = f"{VERSIONS_PREFIX}{decision['decision_id']}/"
        trigger = submit_aml_job(settings, decision["decision_id"], prefix, pointer["prefix"] if pointer else None)
        decision["aml_trigger"] = {"attempted": True, **trigger}
        if trigger["triggered"]:
            write_json_file(
                models_fs,
                PENDING_PATH,
                {
                    "job": trigger["detail"],
                    "decision_id": decision["decision_id"],
                    "prefix": prefix,
                    "submitted_at": checked_at.isoformat(),
                    "trained": {p: f["rows"] for p, f in state.items() if f["valid"]},
                },
            )
    elif should:
        decision["aml_trigger"]["detail"] = "AML_TRIGGER_ENABLED is not true; decision recorded only"

    stamp = checked_at.strftime("%Y%m%dT%H%M%SZ")
    write_json_file(models_fs, f"{DECISION_PREFIX}{stamp}.json", decision)
    write_json_file(models_fs, BASELINE_PATH, {**meta, "files": state, "last_check": decision["checked_at_utc"]})
    return decision


def aml_client(settings: dict):
    from azure.ai.ml import MLClient
    from azure.identity import DefaultAzureCredential

    return MLClient(DefaultAzureCredential(), settings["subscription_id"], settings["resource_group"], settings["workspace"])


def aml_job_status(settings: dict, job_name: str) -> str:
    try:
        status = aml_client(settings).jobs.get(job_name).status
    except Exception as exc:
        logger.warning("Could not read job %s (%s).", job_name, type(exc).__name__)
        return "failed" if type(exc).__name__ == "ResourceNotFoundError" else "running"
    if status == "Completed":
        return "completed"
    if status in {"Failed", "Canceled", "NotResponding"}:
        return "failed"
    return "running"


def submit_aml_job(settings: dict, decision_id: str, prefix: str, current_prefix: str | None) -> dict:
    try:
        from azure.ai.ml import Input, Output, command

        command_line = (
            "pip install -q uv && uv venv -p 3.11 /tmp/v && uv pip install -q -p /tmp/v/bin/python -r requirements.txt "
            "&& /tmp/v/bin/python train.py --data ${{inputs.data}} --out ${{outputs.model}}"
        )
        inputs = {"data": Input(type="uri_folder", path="azureml://datastores/raw/paths/")}
        if current_prefix:
            command_line += " --current ${{inputs.current}}"
            inputs["current"] = Input(type="uri_folder", path=f"azureml://datastores/models/paths/{current_prefix}")
        job = command(
            code=settings.get("code_dir", "training"),
            command=command_line,
            inputs=inputs,
            outputs={"model": Output(type="uri_folder", path=f"azureml://datastores/models/paths/{prefix}", mode="upload")},
            environment="azureml://registries/azureml/environments/sklearn-1.5/labels/latest",
            compute=settings["compute"],
            display_name=f"churn-retrain-{decision_id}",
            experiment_name="churn-retrain",
        )
        return {"triggered": True, "detail": aml_client(settings).jobs.create_or_update(job).name}
    except Exception as exc:
        logger.warning("AML submit failed (%s: %s).", type(exc).__name__, exc)
        return {"triggered": False, "detail": f"{type(exc).__name__}: {exc}"}


def prune_old_decisions(fs_client, retention_days: int, now: datetime.datetime | None = None) -> list[str]:
    cutoff = (now or utcnow()) - datetime.timedelta(days=max(0, retention_days))
    deleted = []
    for props in fs_client.get_paths(recursive=False):
        name = getattr(props, "name", "")
        modified = getattr(props, "last_modified", None)
        if getattr(props, "is_directory", False) or not name.startswith(DECISION_PREFIX):
            continue
        if modified is None or modified >= cutoff:
            continue
        try:
            fs_client.delete_file(name)
            deleted.append(name)
        except Exception as exc:
            logger.warning("Could not delete %s (%s).", name, type(exc).__name__)
    return deleted


def prune_old_versions(fs_client, keep: int = KEEP_VERSIONS) -> list[str]:
    pointer = read_json_file(fs_client, POINTER_PATH) or {}
    folders = [
        p for p in fs_client.get_paths(path=VERSIONS_PREFIX.rstrip("/"), recursive=False)
        if getattr(p, "is_directory", False)
    ]
    folders.sort(key=lambda p: getattr(p, "last_modified", None) or utcnow(), reverse=True)
    deleted = []
    for folder in folders[keep:]:
        if pointer.get("prefix", "").rstrip("/") == folder.name:
            continue
        try:
            fs_client.get_directory_client(folder.name).delete_directory()
            deleted.append(folder.name)
        except Exception as exc:
            logger.warning("Could not delete %s (%s).", folder.name, type(exc).__name__)
    return deleted
