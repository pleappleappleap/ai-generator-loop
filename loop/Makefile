.PHONY: all lint typecheck test build format doc clean

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

doc:
	$(MAKE) -C scorers doc

clean:
	$(MAKE) -C scorers clean
