import datetime
import hashlib
import json
import logging
import os

import azure.functions as func
import retrain

app = func.FunctionApp()
CODE_VERSION = hashlib.sha256(open(retrain.__file__, "rb").read()).hexdigest()[:12]
logger = logging.getLogger("churn-functions")


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.getenv(name) or default)
    except ValueError:
        return default


def _service_client():
    from azure.identity import DefaultAzureCredential
    from azure.storage.filedatalake import DataLakeServiceClient

    account = os.environ["STORAGE_ACCOUNT_NAME"]
    return DataLakeServiceClient(f"https://{account}.dfs.core.windows.net", credential=DefaultAzureCredential())


def _aml_settings() -> dict:
    return {
        "subscription_id": os.getenv("AML_SUBSCRIPTION_ID", ""),
        "resource_group": os.getenv("AML_RESOURCE_GROUP", ""),
        "workspace": os.getenv("AML_WORKSPACE", ""),
        "compute": os.getenv("AML_COMPUTE", "cpu-cluster"),
        "code_dir": os.path.join(os.path.dirname(__file__), "training"),
    }


def _check_for_new_data() -> dict:
    account = os.getenv("STORAGE_ACCOUNT_NAME", "").strip()
    if not account:
        return {"status": "skipped", "reason": "STORAGE_ACCOUNT_NAME not set"}
    try:
        return retrain.run_retrain_check(
            _service_client(),
            storage_account=account,
            threshold=_int_env("RETRAIN_ROW_THRESHOLD", 1000),
            aml_enabled=os.getenv("AML_TRIGGER_ENABLED", "false").lower() == "true",
            aml_settings=_aml_settings(),
        )
    except Exception as exc:
        logger.warning("Retrain check could not start (%s: %s).", type(exc).__name__, exc)
        return {"status": "error", "reason": f"{type(exc).__name__}: {exc}"}


@app.timer_trigger(schedule="0 0 2 * * 0", arg_name="timer", run_on_startup=False, use_monitor=True)
def weekly_retrain_check(timer: func.TimerRequest) -> None:
    logger.info("retrain decision: %s", json.dumps(_check_for_new_data()))


@app.event_grid_trigger(arg_name="event")
def on_raw_upload(event: func.EventGridEvent) -> None:
    logger.info("blob event %s on %s", event.event_type, event.subject)
    logger.info("retrain decision: %s", json.dumps(_check_for_new_data()))


@app.timer_trigger(schedule="0 0 1 * * *", arg_name="timer", run_on_startup=False, use_monitor=True)
def daily_cleanup(timer: func.TimerRequest) -> None:
    if not os.getenv("STORAGE_ACCOUNT_NAME"):
        return
    retention = _int_env("CLEANUP_RETENTION_DAYS", 30)
    try:
        models_fs = _service_client().get_file_system_client(retrain.MODELS_FILE_SYSTEM)
        deleted = retrain.prune_old_decisions(models_fs, retention)
        versions = retrain.prune_old_versions(models_fs)
        logger.info("pruned %d decision file(s) older than %d days and %d old model version(s)", len(deleted), retention, len(versions))
    except Exception as exc:
        logger.warning("cleanup failed (%s: %s)", type(exc).__name__, exc)


@app.route(route="health", methods=["GET"], auth_level=func.AuthLevel.ANONYMOUS)
def health(req: func.HttpRequest) -> func.HttpResponse:
    body = {"status": "ok", "code_version": CODE_VERSION, "time": datetime.datetime.now(datetime.timezone.utc).isoformat()}
    return func.HttpResponse(json.dumps(body), mimetype="application/json")
