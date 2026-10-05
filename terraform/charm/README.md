# Trino Gateway charm module

Deploys exactly one `trino-gateway-k8s` charm application and, once the charm exposes
provided endpoints, optionally Juju offers for them. This module owns only the charm deployment;
integrations and models belong to a future product module.

## Prerequisites

The caller must configure an authenticated `juju` provider (controller and credentials) before
using this module. The module accepts a `model_uuid` and does not create, select, or authenticate
against a model or controller itself.

## Kubernetes-only exception

`trino-gateway-k8s` is a Kubernetes charm. This module intentionally omits the `machines` input
present in machine-charm modules; there is no way to target specific machines for a Kubernetes
application.

## Inputs

| Name | Type | Default | Nullable | Description |
| --- | --- | --- | --- | --- |
| `app_name` | `string` | `"trino-gateway-k8s"` | No | Application name for the deployment. |
| `base` | `string` | `null` | Yes | Operating system base passed to the charm block. |
| `channel` | `string` | `"latest/edge"` | No | Charmhub channel to deploy from. |
| `config` | `map(string)` | `{}` | No | Charm configuration options, passed unchanged, such as `backends`. |
| `constraints` | `string` | `null` | Yes | Juju deployment constraints. |
| `endpoint_bindings` | `set(object({ space = string, endpoint = optional(string) }))` | `[]` | No | Network space bindings; omitted `endpoint` binds the application default. |
| `expose` | `object({ cidrs = optional(string), endpoints = optional(string), spaces = optional(string) })` | `null` | Yes | `null` omits exposure; `{}` exposes all endpoints. |
| `model_uuid` | `string` | None | No | Target Juju model UUID. Required. |
| `offered_endpoints` | `list(string)` | `[]` | No | Provided endpoints to offer. Must be empty while the charm provides none. |
| `resources` | `map(string)` | `{}` | No | Resource/image overrides, including `trino-gateway-image`. Empty uses resources bundled with the selected charm revision. |
| `revision` | `number` | `null` | Yes | Charm revision. Null selects the latest revision on `channel`. |
| `storage_directives` | `map(string)` | `{}` | No | Juju storage directives for the application. |
| `trust` | `bool` | `true` | No | Grants the application trust to interact with Kubernetes. |
| `units` | `number` | `1` | No | Number of units. Must be at least `1`. |

## Outputs

| Name | Description |
| --- | --- |
| `application` | The full `juju_application` resource. |
| `offers` | Map keyed by offered endpoint: `{ kind = "offer", url = <offer URL> }`. Empty while the charm provides no endpoints. |
| `provides` | Map of provided endpoint objects. Empty while the charm declares no provided endpoints. |
| `requires` | Map of required endpoint objects, keyed by endpoint: `postgresql` (`postgresql_client` interface). |

## Resource overrides

Set `resources = { "trino-gateway-image" = "<revision-or-oci-url>" }` to pin a specific
resource. Leaving `resources` empty (the default) uses whatever resource Juju resolves for the
deployed charm revision.

## Example

```hcl
module "trino_gateway" {
  source = "./terraform/charm"

  model_uuid = juju_model.this.uuid
}
```

## Testing

Run from this directory:

```shell
terraform init
terraform fmt -check
terraform validate
terraform test
```
