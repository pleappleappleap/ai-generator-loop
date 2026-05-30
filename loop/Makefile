.PHONY: all lint typecheck test format docs clean

all: lint typecheck test

lint:
	$(MAKE) -C scorers lint
	scorers/venv/bin/ruff check ui/

typecheck:
	$(MAKE) -C scorers typecheck
	cd ui && ../scorers/venv/bin/pyright .

test:
	$(MAKE) -C scorers test

format:
	$(MAKE) -C scorers format
	scorers/venv/bin/ruff format ui/

docs:
	$(MAKE) -C scorers docs

clean:
	$(MAKE) -C scorers clean
	rm -rf __pycache__
	rm -f *.pyc
