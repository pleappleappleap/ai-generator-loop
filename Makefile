.PHONY: all lint typecheck test build format doc clean config check

all: lint typecheck test build

lint:
	$(MAKE) -C loop lint
	$(MAKE) -C tactical-llm lint

typecheck:
	$(MAKE) -C loop typecheck
	$(MAKE) -C tactical-llm typecheck

test:
	$(MAKE) -C loop test
	$(MAKE) -C tactical-llm test

build:
	$(MAKE) -C loop build

format:
	$(MAKE) -C loop format

doc:
	latexmk -pdf -interaction=nonstopmode ARCHITECTURE.tex
	$(MAKE) -C loop doc
	$(MAKE) -C tactical-llm doc

clean:
	latexmk -C ARCHITECTURE.tex
	rm -f *.aux *.log *.out *.toc *.fls *.fdb_latexmk
	$(MAKE) -C loop clean
	$(MAKE) -C tactical-llm clean

# Interactive configuration TUI (menuconfig-style).
# Generates config.yaml and config.sh from auto-detected defaults.
config:
	python3 menuconfig.py

# Validate the environment against the current config.yaml.
# Checks services, GPU backend, model files, venvs, and Rust binaries.
check: config.yaml
	python3 check_env.py

config.yaml:
	@echo "config.yaml not found — running make config first"
	$(MAKE) config
