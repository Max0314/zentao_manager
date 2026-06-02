PYTHON ?= python3
DOCKER_IMAGE ?= zentao-manager:local

.DEFAULT_GOAL := help

.PHONY: help install test compile docker-build check

help:
	@echo "Zentao Manager commands:"
	@echo "  make install       Install Python dependencies"
	@echo "  make test          Run pytest suite"
	@echo "  make compile       Compile-check Python files"
	@echo "  make docker-build  Build Docker image"
	@echo "  make check         Run test and compile"

install:
	$(PYTHON) -m pip install -r requirements.txt

test:
	$(PYTHON) -m pytest -q tests

compile:
	$(PYTHON) -m compileall -q app scripts

docker-build:
	docker build -t $(DOCKER_IMAGE) .

check: test compile
