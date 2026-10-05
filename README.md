# trino-gateway-k8s-operator

This repository contains the Kubernetes charm for Trino Gateway, a load balancer, proxy, and
configurable routing gateway for multiple Trino clusters.

The charm is named `trino-gateway-k8s` on Charmhub and deploys on Kubernetes with Juju 3.6 or
later. It is currently a work in progress and is not ready for production use.

## Deploy

Trino Gateway stores its state in PostgreSQL, so the charm requires a `postgresql` integration
using the `postgresql_client` interface:

```shell
juju deploy trino-gateway-k8s --channel latest/edge --trust
juju deploy postgresql-k8s --channel 14/stable --trust
juju integrate trino-gateway-k8s postgresql-k8s
```

Until the integration is ready, the unit is Blocked with
`missing required relation: postgresql` and the gateway is not started. Once the database is
available, the charm writes the gateway configuration, the gateway creates its schema on
startup, and the unit becomes Active. The gateway serves its web UI, REST API and Trino proxy on
port `8080`.

Credential rotation and endpoint changes from PostgreSQL are applied automatically by
restarting the gateway.

## Limitations

- Only a single unit is supported.
- There is no TLS and no ingress; the gateway is reachable over plain HTTP inside the cluster.
- The gateway's admin web UI and REST API are not authenticated. Anyone who can reach port
  `8080` can change the gateway's state.
- If the `postgresql` integration is removed, the unit becomes Blocked but the gateway keeps
  running, so clients get errors from the gateway rather than refused connections.

## Other resources

- [Charmhub](https://charmhub.io/trino-gateway-k8s)
- [Contributing](CONTRIBUTING.md)
- [Juju documentation](https://documentation.ubuntu.com/juju/3.6/howto/manage-charms/)
