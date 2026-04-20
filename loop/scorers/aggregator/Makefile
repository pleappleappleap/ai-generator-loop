.PHONY: all lint typecheck test build format doc clean

all: lint test build

lint:
	cargo clippy -- -D warnings

typecheck:
	cargo check

test:
	cargo test

format:
	cargo fmt

build:
	cargo build --release

doc:
	cargo doc --no-deps

clean:
	cargo clean
