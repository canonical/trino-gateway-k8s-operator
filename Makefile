# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

SHELL := /bin/bash
.SHELLFLAGS := -eu -o pipefail -c

.PHONY: all build clean fmt lint static unit integration test deploy help

all: build

build:
	opcli artifacts build

clean:
	rm -rf build

fmt:
	tox -e fmt

lint:
	tox -e lint

static:
	tox -e static

unit:
	tox -e unit

integration:
	opcli spread run

test: fmt lint static unit

deploy:
	opcli env provision
	opcli artifacts push-images --missing-registry deploy
	opcli pytest run

help:
	@printf '%s\n' 'Targets: build clean fmt lint static unit integration test deploy'
