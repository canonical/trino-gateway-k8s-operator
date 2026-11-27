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

<!-- BEGIN_TF_DOCS -->
## Requirements

| Name | Version |
|------|---------|
| <a name="requirement_terraform"></a> [terraform](#requirement\_terraform) | >= 1.14 |
| <a name="requirement_juju"></a> [juju](#requirement\_juju) | ~> 2.0 |

## Providers

| Name | Version |
|------|---------|
| <a name="provider_juju"></a> [juju](#provider\_juju) | ~> 2.0 |

## Modules

No modules.

## Resources

| Name | Type |
|------|------|
| [juju_application.this](https://registry.terraform.io/providers/juju/juju/latest/docs/resources/application) | resource |
| [juju_offer.this](https://registry.terraform.io/providers/juju/juju/latest/docs/resources/offer) | resource |

## Inputs

| Name | Description | Type | Default | Required |
|------|-------------|------|---------|:--------:|
| <a name="input_app_name"></a> [app\_name](#input\_app\_name) | Application name for the Trino Gateway deployment. | `string` | `"trino-gateway-k8s"` | no |
| <a name="input_base"></a> [base](#input\_base) | Operating system base passed to the charm block. | `string` | `null` | no |
| <a name="input_channel"></a> [channel](#input\_channel) | Charmhub channel to deploy Trino Gateway from. | `string` | `"latest/edge"` | no |
| <a name="input_config"></a> [config](#input\_config) | Charm configuration options passed to the application unchanged, such as `catalog-exclusions`. | `map(string)` | `{}` | no |
| <a name="input_constraints"></a> [constraints](#input\_constraints) | Juju deployment constraints for the application. | `string` | `null` | no |
| <a name="input_endpoint_bindings"></a> [endpoint\_bindings](#input\_endpoint\_bindings) | Network space bindings for the application's endpoints. An entry without `endpoint` binds the application default. | <pre>set(object({<br/>    space    = string<br/>    endpoint = optional(string)<br/>  }))</pre> | `[]` | no |
| <a name="input_expose"></a> [expose](#input\_expose) | Exposure configuration. The default empty object exposes all endpoints; null leaves the application unexposed. | <pre>object({<br/>    cidrs     = optional(string)<br/>    endpoints = optional(string)<br/>    spaces    = optional(string)<br/>  })</pre> | `{}` | no |
| <a name="input_model_uuid"></a> [model\_uuid](#input\_model\_uuid) | UUID of the Juju model to deploy the application into. | `string` | n/a | yes |
| <a name="input_offered_endpoints"></a> [offered\_endpoints](#input\_offered\_endpoints) | Provided endpoints to offer for cross-model integration: trino-catalog and trino-gateway-integration. | `list(string)` | `[]` | no |
| <a name="input_resources"></a> [resources](#input\_resources) | Charm resource revisions or OCI image overrides, including trino-gateway-image. Empty uses resources bundled with the selected charm revision. | `map(string)` | `{}` | no |
| <a name="input_revision"></a> [revision](#input\_revision) | Charm revision to deploy. Null selects the latest revision on channel. | `number` | `null` | no |
| <a name="input_storage_directives"></a> [storage\_directives](#input\_storage\_directives) | Juju storage directives for the application. | `map(string)` | `{}` | no |
| <a name="input_trust"></a> [trust](#input\_trust) | Whether to grant the application trust to interact with Kubernetes. | `bool` | `true` | no |
| <a name="input_units"></a> [units](#input\_units) | Number of application units to deploy. Must be at least 1. | `number` | `1` | no |

## Outputs

| Name | Description |
|------|-------------|
| <a name="output_application"></a> [application](#output\_application) | The full juju\_application resource for the deployed Trino Gateway charm. |
| <a name="output_offers"></a> [offers](#output\_offers) | Offers keyed by offered endpoint, each `{ kind = "offer", url = <offer URL> }`. Empty unless `offered_endpoints` is set. |
| <a name="output_provides"></a> [provides](#output\_provides) | Provided endpoints keyed by name, each `{ kind = "endpoint", name = <application name>, endpoint = <relation endpoint> }`: `trino_catalog` (`trino_catalog` interface) and `trino_gateway_integration` (`trino_gateway_integration` interface). |
| <a name="output_requires"></a> [requires](#output\_requires) | Required endpoints keyed by name, each `{ kind = "endpoint", name = <application name>, endpoint = <relation endpoint> }`: `postgresql` (`postgresql_client` interface), `backend_trino_catalog` (`trino_catalog` interface, optional) and `ingress` (`ingress` interface, optional). |
<!-- END_TF_DOCS -->
