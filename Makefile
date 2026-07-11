.PHONY: help install test lint coverage clean

help:
	@echo "Targets:"
	@echo "  install   Install the package with test extras"
	@echo "  test      Run the test suite (100% coverage gate)"
	@echo "  lint      Run ruff over the source and tests"
	@echo "  coverage  Run tests and write an HTML coverage report"
	@echo "  clean     Remove caches and build artifacts"

install:
	python -m pip install --upgrade pip
	pip install -e ".[test]"

test:
	pytest

lint:
	ruff check .

coverage:
	pytest --cov-report=html
	@echo "HTML report written to htmlcov/index.html"

clean:
	rm -rf .pytest_cache .coverage htmlcov coverage.xml build dist *.egg-info
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
