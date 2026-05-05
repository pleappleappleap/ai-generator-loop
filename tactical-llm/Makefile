.PHONY: all lint typecheck test docs clean

VENV ?= ../loop/scorers/venv
PYTHON = $(VENV)/bin/python3

all: lint typecheck

lint:
	. $(VENV)/bin/activate &&ruff check .

typecheck:
	. $(VENV)/bin/activate &&pyright .

test:
	. $(VENV)/bin/activate &&pytest tests/ -v

docs:
	. $(VENV)/bin/activate &&pdoc \
		tactical_llm \
		prompts \
		retrieval \
		session \
		-o docs/

clean:
	rm -rf __pycache__ .pytest_cache docs
	rm -f *.pyc
