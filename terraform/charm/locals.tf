# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

locals {
  # null means no expose block; {} means an empty (expose-all) block.
  expose_blocks = var.expose == null ? [] : [var.expose]

  provided_endpoints = {}
  required_endpoints = {}
}
