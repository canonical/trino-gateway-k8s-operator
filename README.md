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

## Configure Trino backends

Register Trino clusters with the `backends` option, a YAML list of entries with a unique `name`
and the `url` of the Trino coordinator:

```shell
juju config trino-gateway-k8s backends='
- name: trino-a
  url: http://trino-k8s.trino-model.svc.cluster.local:8080
'
```

The charm keeps the gateway's registered backends identical to this list. Backends added,
changed or removed through the gateway's API are reverted. All backends are placed in the
`adhoc` routing group and checked with the gateway's default unauthenticated health check
(`/v1/info`). An invalid value leaves the unit Blocked with a message pointing at the problem;
an empty value is valid, and the unit reports `no backends configured`.

Changes are applied by the leader unit on its next hook. After the gateway restarts, the backend
list may only converge on the following `update-status` hook.

## Connect a client

Clients connect to the gateway's Kubernetes Service over HTTP, using their usual Trino
credentials:

```text
http://trino-gateway-k8s.<model>.svc.cluster.local:8080
```

The gateway passes credentials through to the Trino cluster unchanged. Because the connection
is plain HTTP, each Trino cluster must be configured to work behind a proxy:

- `http-server.process-forwarded=true`, so that follow-up URLs point at the gateway rather than
  the cluster.
- `http-server.authentication.allow-insecure-over-http=true`, so that clients can authenticate
  with a user name only. Trino accepts passwords over HTTP only when the forwarded protocol is
  HTTPS, so password authentication through the gateway is not possible until TLS is supported.

The `trino-k8s` charm sets both.

## Limitations

- Only a single unit is supported.
- There is no TLS and no ingress; clients connect over plain HTTP to the in-cluster Service
  address only.
- The gateway's admin web UI and REST API are not authenticated. Anyone who can reach port
  `8080` can change the gateway's state, although the charm reverts backend changes.
- The charm owns the whole backend list and removes backends that are not in `backends`.
- If the `postgresql` integration is removed, the unit becomes Blocked but the gateway keeps
  running, so clients get errors from the gateway rather than refused connections.

## Other resources

- [Charmhub](https://charmhub.io/trino-gateway-k8s)
- [Contributing](CONTRIBUTING.md)
- [Juju documentation](https://documentation.ubuntu.com/juju/3.6/howto/manage-charms/)
