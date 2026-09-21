# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

# tests/setup creates one ephemeral K8s model shared by every plan-only run below.
run "setup" {
  module {
    source = "./tests/setup"
  }
}

run "basic_deploy" {
  command = plan

  variables {
    model_uuid = run.setup.model_uuid
    channel    = "latest/edge"
    # renovate: depName="trino-gateway-k8s"
    revision = 1
  }

  assert {
    condition     = output.application.name == "trino-gateway-k8s"
    error_message = "default app_name did not match expected trino-gateway-k8s"
  }

  assert {
    condition     = length(output.offers) == 0
    error_message = "no offers should be created when offered_endpoints is empty"
  }

  assert {
    condition     = length(output.provides) == 0 && length(output.requires) == 0
    error_message = "Trino Gateway should not report undeclared relation endpoints"
  }
}

run "resources_default" {
  command = plan

  variables {
    model_uuid = run.setup.model_uuid
  }

  assert {
    condition     = length(output.application.resources) == 0
    error_message = "empty resources should use resources bundled with the charm revision"
  }
}

run "resources_override" {
  command = plan

  variables {
    model_uuid = run.setup.model_uuid
    resources  = { "trino-gateway-image" = "docker.io/example/trino-gateway-image:test" }
  }

  assert {
    condition     = output.application.resources["trino-gateway-image"] == "docker.io/example/trino-gateway-image:test"
    error_message = "resource override was not forwarded to the application"
  }
}

run "invalid_offered_endpoint" {
  command = plan

  variables {
    model_uuid        = run.setup.model_uuid
    offered_endpoints = ["not-a-real-endpoint"]
  }

  expect_failures = [var.offered_endpoints]
}
