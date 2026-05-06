.PHONY: all lint typecheck test build format docs clean sqlx-prepare

# Use cargo from rustup install location; override with make CARGO=/path/to/cargo
CARGO ?= $(HOME)/.cargo/bin/cargo

all: lint typecheck test build

lint:
	venv/bin/ruff check .
	$(CARGO) clippy --workspace -- -D warnings

typecheck:
	venv/bin/pyright .

test:
	venv/bin/pytest tests/ -v
	SQLX_OFFLINE=true $(CARGO) test --workspace

format:
	venv/bin/ruff format .
	$(CARGO) fmt --all

build:
	SQLX_OFFLINE=true $(CARGO) build --release --workspace

docs:
	venv/bin/pdoc \
		clip_scorer \
		artifact_scorer \
		vlm_scorer \
		-o docs/
	$(CARGO) doc --no-deps --workspace

clean:
	$(CARGO) clean
	rm -rf __pycache__ tests/__pycache__ .pytest_cache tests/.pytest_cache docs
	rm -f *.pyc tests/*.pyc

# Generate the sqlx offline query cache (.sqlx/).
# Requires sqlx-cli: cargo install sqlx-cli --no-default-features --features sqlite
# Run this once after schema changes, then commit the updated .sqlx/ directory.
sqlx-prepare:
	@echo "==> Creating temporary database for sqlx prepare..."
	@mkdir -p /tmp/sqlx_prepare
	@python3.11 -c "\
import sqlite3; \
db = sqlite3.connect('/tmp/sqlx_prepare/pipeline.db'); \
db.executescript(open('db/schema.sql').read()); \
db.close(); \
print('    Schema applied.')"
	DATABASE_URL=sqlite:////tmp/sqlx_prepare/pipeline.db \
		$(CARGO) sqlx prepare --workspace -- --tests
	@echo "==> sqlx offline cache updated in .sqlx/"
	@echo "    Commit the .sqlx/ directory to git."
