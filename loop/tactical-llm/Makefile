.PHONY: all lint typecheck test docs clean venv

VENV ?= venv
PYTHON = $(VENV)/bin/python3

venv:
	python3 -m venv $(VENV)
	$(VENV)/bin/pip install ruff pyright pytest pytest-mock

all: lint typecheck

lint:
	$(VENV)/bin/ruff check .

typecheck:
	$(VENV)/bin/pyright .

test:
	$(VENV)/bin/pytest tests/ -v

docs:
	PYTHONPATH=. $(VENV)/bin/pdoc \
		prompts \
		-o docs/

clean:
	rm -rf __pycache__ tests/__pycache__ .pytest_cache tests/.pytest_cache .ruff_cache docs venv
	rm -f *.pyc tests/*.pyc
