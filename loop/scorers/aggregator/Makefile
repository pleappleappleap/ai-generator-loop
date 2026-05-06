.PHONY: all lint typecheck test build format docs clean

CARGO ?= $(HOME)/.cargo/bin/cargo

all: lint test build

lint:
	$(CARGO) clippy -- -D warnings

typecheck:
	$(CARGO) check

test:
	SQLX_OFFLINE=true $(CARGO) test

format:
	$(CARGO) fmt

build:
	SQLX_OFFLINE=true $(CARGO) build --release

docs:
	$(CARGO) doc --no-deps

clean:
	$(CARGO) clean
