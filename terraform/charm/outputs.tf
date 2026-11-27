# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

output "application" {
  description = "The full juju_application resource for the deployed Trino Gateway charm."
  value       = juju_application.this
}

output "offers" {
  description = "Offers keyed by offered endpoint, each `{ kind = \"offer\", url = <offer URL> }`. Empty unless `offered_endpoints` is set."
  value = {
    for endpoint, offer in juju_offer.this : endpoint => {
      kind = "offer"
      url  = offer.url
    }
  }
}

output "provides" {
  description = "Provided endpoints keyed by name, each `{ kind = \"endpoint\", name = <application name>, endpoint = <relation endpoint> }`: `trino_catalog` (`trino_catalog` interface) and `trino_gateway_integration` (`trino_gateway_integration` interface)."
  value = {
    for key, endpoint in local.provided_endpoints : key => {
      kind     = "endpoint"
      name     = juju_application.this.name
      endpoint = endpoint
    }
  }
}

output "requires" {
  description = "Required endpoints keyed by name, each `{ kind = \"endpoint\", name = <application name>, endpoint = <relation endpoint> }`: `postgresql` (`postgresql_client` interface), `backend_trino_catalog` (`trino_catalog` interface, optional), `ingress` (`ingress` interface, optional) and `oauth` (`oauth` interface, optional)."
  value = {
    for key, endpoint in local.required_endpoints : key => {
      kind     = "endpoint"
      name     = juju_application.this.name
      endpoint = endpoint
    }
  }
}
