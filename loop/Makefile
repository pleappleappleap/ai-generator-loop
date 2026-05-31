.PHONY: all lint typecheck test format docs clean

all: lint typecheck test

lint:
	$(MAKE) -C scorers lint

typecheck:
	$(MAKE) -C scorers typecheck

test:
	$(MAKE) -C scorers test

format:
	$(MAKE) -C scorers format

docs:
	$(MAKE) -C scorers docs

clean:
	$(MAKE) -C scorers clean
	rm -rf __pycache__ .pytest_cache .ruff_cache
	rm -f *.pyc
