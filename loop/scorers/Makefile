.PHONY: all lint typecheck test build format doc clean

all: lint typecheck test build

lint:
	source venv/bin/activate && ruff check .
	$(MAKE) -C router lint
	$(MAKE) -C aggregator lint

typecheck:
	source venv/bin/activate && pyright .

test:
	source venv/bin/activate && pytest tests/ -v
	$(MAKE) -C router test
	$(MAKE) -C aggregator test

format:
	source venv/bin/activate && ruff format .
	$(MAKE) -C router format
	$(MAKE) -C aggregator format

build:
	$(MAKE) -C router build
	$(MAKE) -C aggregator build

doc:
	source venv/bin/activate && pdoc \
		clip_scorer \
		artifact_scorer \
		vlm_scorer \
		scorer_aggregator \
		-o docs/
	$(MAKE) -C router doc
	$(MAKE) -C aggregator doc

clean:
	$(MAKE) -C router clean
	$(MAKE) -C aggregator clean
	rm -rf __pycache__ .pytest_cache docs
	rm -f *.pyc
