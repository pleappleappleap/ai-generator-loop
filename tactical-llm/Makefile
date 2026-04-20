.PHONY: all lint typecheck test doc clean

VENV ?= ../loop/scorers/venv
PYTHON = $(VENV)/bin/python3

all: lint typecheck

lint:
	source $(VENV)/bin/activate && ruff check .

typecheck:
	source $(VENV)/bin/activate && pyright .

test:
	source $(VENV)/bin/activate && pytest tests/ -v

doc:
	source $(VENV)/bin/activate && pdoc \
		tactical_llm \
		prompts \
		retrieval \
		session \
		-o docs/

clean:
	rm -rf __pycache__ .pytest_cache docs
	rm -f *.pyc
