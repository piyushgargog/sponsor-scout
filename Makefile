# Convenience wrappers. Everything is plain Python; there is no build step.
PY ?= python3

.PHONY: install dev test check seed seed-full worker llm-check event models clean

install:            ## install dependencies
	$(PY) -m pip install -r requirements.txt

dev:                ## dev server + in-process worker on :5000 (login password: demo)
	$(PY) run.py

test:               ## full offline test suite (no credentials needed)
	$(PY) -W ignore -m unittest discover -s tests -t .

check:              ## byte-compile everything (cheap syntax/import sanity)
	$(PY) -m compileall -q app tests run.py

seed:               ## create the demo event only
	$(PY) -m app.cli seed

seed-full:          ## demo event + discovery + research, run synchronously
	$(PY) -m app.cli seed --full

worker:             ## job worker only (use with WORKER_ENABLED=0 on the web process)
	$(PY) -m app.cli worker

event:              ## create an event from a JSON preset and find sponsors: make event FILE=events/your-event.json
	$(PY) -m app.cli create-event $(FILE) --run

llm-check:          ## smoke-test the configured LLM provider (with LLM_PROVIDER=cli this proves the CLI is signed in)
	$(PY) -m app.cli llm-check

models:             ## (API-key mode only) list Gemini models your GEMINI_API_KEY can call
	$(PY) -m app.cli models

clean:              ## remove caches and the local SQLite database
	find . -name __pycache__ -prune -exec rm -rf {} +
	rm -f data/*.db data/*.db-wal data/*.db-shm
