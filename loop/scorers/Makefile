.PHONY: all lint typecheck test format docs clean

all: lint typecheck test

lint:
	venv/bin/ruff check .

typecheck:
	venv/bin/pyright .

test:
	venv/bin/pytest tests/ -v

format:
	venv/bin/ruff format .

docs:
	PYTHONPATH=. venv/bin/pdoc \
		clip_scorer \
		artifact_scorer \
		vlm_scorer \
		-o docs/

clean:
	rm -rf __pycache__ tests/__pycache__ .pytest_cache tests/.pytest_cache docs
	rm -f *.pyc tests/*.pyc
