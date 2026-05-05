.PHONY: all lint typecheck test build format docs clean

all: lint test build

lint:
	cargo clippy -- -D warnings

typecheck:
	cargo check

test:
	SQLX_OFFLINE=true cargo test

format:
	cargo fmt

build:
	SQLX_OFFLINE=true cargo build --release

docs:
	cargo doc --no-deps

clean:
	cargo clean
