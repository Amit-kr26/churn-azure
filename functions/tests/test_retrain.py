import datetime
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import retrain

HEADER = "CustomerId,CreditScore,Geography,Gender,Age,Tenure,Balance,NumOfProducts,HasCrCard,IsActiveMember,EstimatedSalary,Exited"


def csv_bytes(n, start=0):
    rows = [f"{start + i},600,France,Male,40,3,1000,1,1,1,50000,0" for i in range(n)]
    return ("\n".join([HEADER, *rows]) + "\n").encode()


def utc(day=1):
    return datetime.datetime(2026, 1, day, tzinfo=datetime.timezone.utc)


class FakeDownload:
    def __init__(self, data):
        self._data = data

    def readall(self):
        return self._data

    def chunks(self):
        for i in range(0, len(self._data), 37):
            yield self._data[i : i + 37]


class NotFound(Exception):
    pass


NotFound.__name__ = "ResourceNotFoundError"


class FakeLease:
    def __init__(self, fs, path):
        self.fs, self.path, self.renewed = fs, path, 0

    def release(self):
        self.fs.leases.discard(self.path)

    def renew(self):
        self.renewed += 1


class FakeFileClient:
    def __init__(self, fs, path):
        self.fs, self.path = fs, path

    def download_file(self):
        if self.path not in self.fs.store:
            raise NotFound(self.path)
        return FakeDownload(self.fs.store[self.path])

    def upload_data(self, data, overwrite=False):
        self.fs.store[self.path] = data

    def get_file_properties(self):
        if self.path not in self.fs.store:
            raise NotFound(self.path)

    def create_file(self):
        self.fs.store[self.path] = b""

    def acquire_lease(self, lease_duration=60):
        if self.path in self.fs.leases:
            raise RuntimeError("lease held")
        self.fs.leases.add(self.path)
        return FakeLease(self.fs, self.path)


class FakeProps:
    def __init__(self, name, etag=None, is_directory=False, last_modified=None):
        self.name, self.etag, self.is_directory, self.last_modified = name, etag, is_directory, last_modified


class FakeFS:
    def __init__(self):
        self.store, self.leases, self.listing = {}, set(), []

    def get_paths(self, path=None, recursive=True):
        return self.listing

    def get_file_client(self, path):
        return FakeFileClient(self, path)

    def delete_file(self, path):
        if path not in self.store:
            raise NotFound(path)
        del self.store[path]


class FakeService:
    def __init__(self, raw, models):
        self.fs = {"raw": raw, "models": models}

    def get_file_system_client(self, name):
        return self.fs[name]


def make(files):
    raw, models = FakeFS(), FakeFS()
    for name, etag, data in files:
        raw.store[name] = data
        raw.listing.append(FakeProps(name, etag))
    return FakeService(raw, models), raw, models


def add_file(raw, name, etag, data):
    raw.store[name] = data
    raw.listing[:] = [p for p in raw.listing if p.name != name] + [FakeProps(name, etag)]


def check(service, **kw):
    args = {"storage_account": "t", "threshold": 1000, "aml_enabled": False, "now": utc()}
    return retrain.run_retrain_check(service, **{**args, **kw})


def test_count_csv_handles_quoted_newlines_and_blank_lines():
    text = HEADER + '\n1,600,France,"Mal\ne",40,3,1,1,1,1,5,0\n\n2,600,France,Male,40,3,1,1,1,1,5,0\n'
    valid, rows = retrain.count_csv(retrain.iter_lines([text.encode()]))
    assert (valid, rows) == (True, 2)


def test_file_without_required_columns_is_invalid():
    valid, rows = retrain.count_csv(retrain.iter_lines([b"CreditScore,Age\n600,40\n"]))
    assert (valid, rows) == (False, 0)


def test_rows_accumulate_across_checks_until_threshold():
    service, raw, _models = make([("a.csv", "1", csv_bytes(600))])
    assert check(service)["new_rows"] == 600
    add_file(raw, "b.csv", "2", csv_bytes(600, 1000))
    decision = check(service)
    assert decision["new_rows"] == 1200
    assert decision["should_retrain"] is True


def test_appended_file_counts_only_the_new_rows():
    service, raw, _ = make([("a.csv", "1", csv_bytes(500))])
    check(service)
    add_file(raw, "a.csv", "2", csv_bytes(800))
    assert check(service)["new_rows"] == 800


def test_invalid_file_is_reported_and_not_counted():
    service, _, _ = make([("ok.csv", "1", csv_bytes(300)), ("bad.csv", "2", b"x,y\n1,2\n")])
    decision = check(service)
    assert decision["new_rows"] == 300
    assert decision["invalid_files"] == ["bad.csv"]


def test_submission_writes_pending_and_not_progress(monkeypatch):
    service, _, models = make([("a.csv", "1", csv_bytes(1200))])
    monkeypatch.setattr(retrain, "submit_aml_job", lambda s, d, p, c: {"triggered": True, "detail": "job-1"})
    decision = check(service, aml_enabled=True, job_status=lambda s, j: "running")
    assert decision["aml_trigger"]["triggered"] is True
    pending = json.loads(models.store[retrain.PENDING_PATH])
    assert pending["job"] == "job-1" and pending["trained"] == {"a.csv": 1200}
    baseline = json.loads(models.store[retrain.BASELINE_PATH])
    assert baseline["files"]["a.csv"]["trained_rows"] == 0


def test_failed_submission_keeps_rows_for_the_next_check(monkeypatch):
    service, _, models = make([("a.csv", "1", csv_bytes(1200))])
    monkeypatch.setattr(retrain, "submit_aml_job", lambda s, d, p, c: {"triggered": False, "detail": "boom"})
    check(service, aml_enabled=True)
    assert retrain.PENDING_PATH not in models.store
    assert check(service, aml_enabled=True, now=utc(2))["new_rows"] == 1200


def test_second_check_waits_while_job_runs(monkeypatch):
    service, _, _models = make([("a.csv", "1", csv_bytes(1200))])
    calls = []
    monkeypatch.setattr(retrain, "submit_aml_job", lambda s, d, p, c: calls.append(p) or {"triggered": True, "detail": "job-1"})
    check(service, aml_enabled=True, job_status=lambda s, j: "running")
    decision = check(service, aml_enabled=True, job_status=lambda s, j: "running", now=utc(2))
    assert decision["status"] == "waiting"
    assert len(calls) == 1


def test_completed_job_publishes_pointer_and_advances_baseline(monkeypatch):
    service, _, models = make([("a.csv", "1", csv_bytes(1200))])
    prefixes = []
    monkeypatch.setattr(retrain, "submit_aml_job", lambda s, d, p, c: prefixes.append(p) or {"triggered": True, "detail": "job-1"})
    check(service, aml_enabled=True, job_status=lambda s, j: "running")
    models.store[f"{prefixes[0]}manifest.json"] = json.dumps({"created_utc": "2026-01-02T00:00:00+00:00", "model_version": "v2"}).encode()
    decision = check(service, aml_enabled=True, job_status=lambda s, j: "completed", now=utc(2))
    pointer = json.loads(models.store[retrain.POINTER_PATH])
    assert pointer["prefix"] == prefixes[0] and pointer["model_version"] == "v2"
    assert retrain.PENDING_PATH not in models.store
    assert decision["new_rows"] == 0


def test_rejected_model_moves_baseline_but_not_pointer(monkeypatch):
    service, _, models = make([("a.csv", "1", csv_bytes(1200))])
    monkeypatch.setattr(retrain, "submit_aml_job", lambda s, d, p, c: {"triggered": True, "detail": "job-1"})
    check(service, aml_enabled=True, job_status=lambda s, j: "running")
    decision = check(service, aml_enabled=True, job_status=lambda s, j: "completed", now=utc(2))
    assert retrain.POINTER_PATH not in models.store
    assert decision["new_rows"] == 0


def test_failed_job_keeps_rows_and_backs_off(monkeypatch):
    service, _, _models = make([("a.csv", "1", csv_bytes(1200))])
    submitted = []
    monkeypatch.setattr(retrain, "submit_aml_job", lambda s, d, p, c: submitted.append(1) or {"triggered": True, "detail": "job-1"})
    check(service, aml_enabled=True, job_status=lambda s, j: "running")
    decision = check(service, aml_enabled=True, job_status=lambda s, j: "failed", now=utc(1).replace(hour=1))
    assert decision["new_rows"] == 1200
    assert decision["status"] == "backoff"
    later = check(service, aml_enabled=True, job_status=lambda s, j: "failed", now=utc(3))
    assert later["status"] == "ok"
    assert len(submitted) == 2


def test_current_model_prefix_is_passed_to_the_job(monkeypatch):
    service, _, models = make([("a.csv", "1", csv_bytes(1200))])
    models.store[retrain.POINTER_PATH] = json.dumps({"prefix": "versions/old/"}).encode()
    seen = []
    monkeypatch.setattr(retrain, "submit_aml_job", lambda s, d, p, c: seen.append(c) or {"triggered": True, "detail": "j"})
    check(service, aml_enabled=True, job_status=lambda s, j: "running")
    assert seen == ["versions/old/"]


def test_concurrent_check_is_skipped_while_lock_held():
    service, _, models = make([("a.csv", "1", csv_bytes(5))])
    models.leases.add(retrain.LOCK_PATH)
    assert check(service)["status"] == "busy"
    assert not any(k.startswith(retrain.DECISION_PREFIX) for k in models.store)


def test_lock_is_released_and_release_errors_do_not_fail_the_check(monkeypatch):
    service, _, models = make([("a.csv", "1", csv_bytes(5))])
    assert check(service)["status"] == "ok"
    assert retrain.LOCK_PATH not in models.leases

    def broken_release(self):
        raise RuntimeError("lease expired")

    monkeypatch.setattr(FakeLease, "release", broken_release)
    assert check(service, now=utc(2))["status"] == "ok"


def test_storage_error_is_reported_not_swallowed():
    service, raw, _ = make([("a.csv", "1", csv_bytes(5))])

    def boom(path):
        raise PermissionError("403")

    raw.get_file_client = boom
    assert check(service)["status"] == "error"


def test_prune_old_decisions():
    fs = FakeFS()
    old = utc() - datetime.timedelta(days=40)
    fs.listing = [
        FakeProps("retrain_decision-old.json", last_modified=old),
        FakeProps("retrain_decision-new.json", last_modified=utc() + datetime.timedelta(days=1)),
        FakeProps("retrain_baseline.json", last_modified=old),
    ]
    for p in fs.listing:
        fs.store[p.name] = b"{}"
    assert retrain.prune_old_decisions(fs, 30, now=utc(1).replace(month=2)) == ["retrain_decision-old.json"]


def test_read_json_file_missing_and_corrupt():
    fs = FakeFS()
    assert retrain.read_json_file(fs, "nope.json") is None
    fs.store["bad.json"] = b"not json"
    assert retrain.read_json_file(fs, "bad.json") is None
