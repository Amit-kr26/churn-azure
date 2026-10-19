VENV := .venv
PY := $(VENV)/bin/python

.PHONY: install train api test lint docker-build

install:
	python3 -m venv $(VENV)
	$(PY) -m pip install -r api/requirements-dev.txt -r training/requirements.txt -r functions/requirements.txt ruff

train:
	$(PY) training/train.py --data data/raw/churn.csv --out api/model

api:
	$(VENV)/bin/uvicorn api.app:app --port 8000

test:
	$(PY) -m pytest api/tests functions/tests training/tests -q

lint:
	$(VENV)/bin/ruff check .

docker-build:
	docker build -t churn-api:latest ./api
