.PHONY: all lint typecheck test build format docs clean sqlx-prepare

all: lint typecheck test build

lint:
	$(MAKE) -C scorers lint

typecheck:
	$(MAKE) -C scorers typecheck

test:
	$(MAKE) -C scorers test

build:
	$(MAKE) -C scorers build

format:
	$(MAKE) -C scorers format

docs:
	$(MAKE) -C scorers docs

sqlx-prepare:
	$(MAKE) -C scorers sqlx-prepare

clean:
	$(MAKE) -C scorers clean
	rm -rf __pycache__
	rm -f *.pyc
