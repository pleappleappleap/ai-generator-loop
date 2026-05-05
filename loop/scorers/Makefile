.PHONY: all lint typecheck test build format docs clean sqlx-prepare

export PATH := $(HOME)/.cargo/bin:$(PATH)

all: lint typecheck test build

lint:
	. venv/bin/activate &&ruff check .
	cargo clippy --workspace -- -D warnings

typecheck:
	. venv/bin/activate &&pyright .

test:
	. venv/bin/activate &&pytest tests/ -v
	SQLX_OFFLINE=true cargo test --workspace

format:
	. venv/bin/activate &&ruff format .
	cargo fmt --all

build:
	SQLX_OFFLINE=true cargo build --release --workspace

docs:
	. venv/bin/activate &&pdoc \
		clip_scorer \
		artifact_scorer \
		vlm_scorer \
		-o docs/
	cargo doc --no-deps --workspace

clean:
	cargo clean
	rm -rf __pycache__ .pytest_cache docs
	rm -f *.pyc

# Generate the sqlx offline query cache (.sqlx/).
# Requires sqlx-cli: cargo install sqlx-cli --no-default-features --features sqlite
# Run this once after schema changes, then commit the updated .sqlx/ directory.
sqlx-prepare:
	@echo "==> Creating temporary database for sqlx prepare..."
	@mkdir -p /tmp/sqlx_prepare
	@python3 -c "\
import sqlite3; \
db = sqlite3.connect('/tmp/sqlx_prepare/pipeline.db'); \
db.executescript(open('db/schema.sql').read()); \
db.close(); \
print('    Schema applied.')"
	DATABASE_URL=sqlite:////tmp/sqlx_prepare/pipeline.db \
		cargo sqlx prepare --workspace
	@echo "==> sqlx offline cache updated in .sqlx/"
	@echo "    Commit the .sqlx/ directory to git."
