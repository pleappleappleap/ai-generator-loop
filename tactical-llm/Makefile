.PHONY: all lint typecheck test docs clean

VENV ?= ../loop/scorers/venv
PYTHON = $(VENV)/bin/python3

all: lint typecheck

lint:
	$(VENV)/bin/ruff check .

typecheck:
	$(VENV)/bin/pyright .

test:
	$(VENV)/bin/pytest tests/ -v

docs:
	PYTHONPATH=. $(VENV)/bin/pdoc \
		tactical_llm \
		prompts \
		retrieval \
		session \
		-o docs/

clean:
	rm -rf __pycache__ tests/__pycache__ .pytest_cache tests/.pytest_cache docs
	rm -f *.pyc tests/*.pyc
