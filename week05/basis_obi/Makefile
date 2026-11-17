PY := .venv/bin/python

.PHONY: collect build test

collect:            ## 수집 (오늘 15:50까지, 끝나면 Parquet·품질 로그)
	$(PY) -m collector.collect

build:              ## 받은 원문 → data/raw/*.parquet, data/quality/*.json
	$(PY) -m collector.build

test:
	$(PY) -m pytest -q tests
