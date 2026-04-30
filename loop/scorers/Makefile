.PHONY: all lint typecheck test build format doc clean

all: lint typecheck test build

lint:
	. venv/bin/activate &&ruff check .
	$(MAKE) -C router lint
	$(MAKE) -C aggregator lint

typecheck:
	. venv/bin/activate &&pyright .

test:
	. venv/bin/activate &&pytest tests/ -v
	$(MAKE) -C router test
	$(MAKE) -C aggregator test

format:
	. venv/bin/activate &&ruff format .
	$(MAKE) -C router format
	$(MAKE) -C aggregator format

build:
	$(MAKE) -C router build
	$(MAKE) -C aggregator build

doc:
	. venv/bin/activate &&pdoc \
		clip_scorer \
		artifact_scorer \
		vlm_scorer \
		-o docs/
	$(MAKE) -C router doc
	$(MAKE) -C aggregator doc

clean:
	$(MAKE) -C router clean
	$(MAKE) -C aggregator clean
	rm -rf __pycache__ .pytest_cache docs
	rm -f *.pyc
