.PHONY: all lint typecheck clean venv

VENV ?= venv
PYTHON = $(VENV)/bin/python3

venv:
	python3 -m venv $(VENV)
	$(VENV)/bin/pip install ruff pyright

all: lint typecheck

lint:
	$(VENV)/bin/ruff check .

typecheck:
	$(VENV)/bin/pyright .

clean:
	rm -rf __pycache__ .pytest_cache .ruff_cache docs venv
	rm -f *.pyc
