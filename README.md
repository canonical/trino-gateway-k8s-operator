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

## Relate Trino applications

Trino Gateway registers Trino applications as backends. A Trino application needs two
relations: `backend-trino-catalog`, which tells the gateway its address and catalogs, and
`trino-gateway-integration`, which lets it accept the gateway's consumer users. This is the
only way to add a backend; the charm has no option for it:

```shell
juju integrate trino-gateway-k8s:backend-trino-catalog trino-k8s:trino-catalog
juju integrate trino-gateway-k8s:trino-gateway-integration trino-k8s:trino-gateway-integration
```

The Trino application must be a coordinator (`charm-function=all` or `coordinator`); relating
a worker application to the gateway is not supported. It must also be a `trino-k8s` revision
that provides `trino-gateway-integration`. A Trino application related over
`backend-trino-catalog` only is never registered.

For a Trino application in another model, offer both endpoints, consume the offer and integrate
with the resulting application:

```shell
juju offer trino-model.trino-k8s:trino-catalog,trino-gateway-integration trino-k8s
juju consume trino-model.trino-k8s trino-a
juju integrate trino-gateway-k8s:backend-trino-catalog trino-a:trino-catalog
juju integrate trino-gateway-k8s:trino-gateway-integration trino-a:trino-gateway-integration
```

The two relations are matched by the address the Trino application publishes on both. The
charm keeps the gateway's registered backends identical to the related applications it
admits, as described in [Connect consumers](#connect-consumers). Backends added, changed or
removed through the gateway's API are reverted. All backends are
placed in the `adhoc` routing group and checked with the gateway's default unauthenticated
health check (`/v1/info`). With no related Trino applications, the unit reports
`no backends related`. Changes are applied by the leader unit on its next hook. After the gateway
restarts, the backend list may only converge on the following `update-status` hook.

- The backend is named after the related application, which for an offer is the name given to
  `juju consume`. A name longer than 63 characters is shortened and given a hash suffix. If a name is still not a valid backend name, the relation adds no backend and
  is reported as not ready.
- The backend URL is inferred from the address the Trino application advertises: HTTPS when the
  port is `443`, HTTP otherwise.
- A relation whose data is missing or malformed adds no backend and does not block the unit. The
  Active message reports how many relations are not ready, and the reason is in the logs.
- The gateway can only reach a Trino application in another Kubernetes cluster if that
  application is exposed through ingress; otherwise it advertises an in-cluster address and the
  backend is registered but unhealthy.
- The credentials in the relation are not used.

## Connect consumers

The charm provides `trino-catalog` for consumers such as Superset and DataHub, so they can use
the gateway in place of a Trino application:

```shell
juju integrate superset-k8s:trino-catalog trino-gateway-k8s:trino-catalog
```

The leader issues one Trino user per `trino-catalog` relation, stores its credentials in a Juju
secret, and requests every issued user from each Trino application related over
`trino-gateway-integration`. Usernames start with `gw-` and stay the same for the lifetime of
the relation; a user is withdrawn when its relation is removed. A Trino application confirms a
user once the user can log in on it.

A consumer is **published** once the gateway has written its connection data to the relation:

- the gateway's in-cluster address, `trino-gateway-k8s.<model>.svc.cluster.local:8080`;
- the union of the catalogs of the registered Trino applications that confirmed its user,
  minus any catalogs excluded for it;
- the ID of its credentials secret, which is granted to that relation only.

The data is written as a whole: a consumer's relation holds either all of it or nothing. Once
published, a consumer stays published until its relation is removed, and its data is updated
when the upstreams, their catalogs, the exclusions or its credentials change.

All backends share one routing group, so any query can reach any registered Trino application.
To never take service away from consumers that already work, the gateway follows these rules:

- A new consumer is published once every registered Trino application has confirmed it. With
  no registered applications it is published straight away, with no catalogs.
- A new Trino application is registered once it has confirmed every published consumer, and
  none of its catalogs conflicts with a catalog of a registered application. Until then it
  does not change any consumer's data.
- A registered Trino application that withdraws or refuses a published consumer's user stays
  registered while it still confirms at least one published consumer. That consumer loses the
  application's catalogs, and the gap is reported in the status.
- A consumer must publish its `app_name`, as consumers using the `trino_catalog` library do.

The leader unit reports progress in its status:

| Status | Meaning |
| --- | --- |
| Active `N upstreams waiting for registration` | Trino applications that are not registered yet. The logs say why. |
| Active `N consumers waiting for publication` | Consumers that have not been published yet. |
| Active `N confirmation gaps` | Registered applications that have not confirmed a published consumer. |
| Blocked `N users refused by M upstreams: <reasons>` | A Trino application refused users. Each reason is an operator error, such as a `gw-*` user configured on Trino, or a bug. |
| Blocked `N catalog conflicts between upstreams` | Catalogs with the same name use different connectors on different applications. Align their `catalog-config`. |
| Blocked `invalid catalog-exclusions: <error>` | The option is not valid YAML. |

Blocked does not mean the gateway is down: consumers that already work keep working.

### Exclude catalogs

The `catalog-exclusions` option hides catalogs from consumers, the same way as in the
`trino-k8s` charm. It is a YAML mapping of the `app_name` each consumer publishes, usually its
application name, to the catalogs to hide from it:

```shell
juju config trino-gateway-k8s catalog-exclusions='superset-k8s: [catalog_developer]'
```

Entries that are not lists are ignored. If the value is not valid YAML, the unit is Blocked and
no consumer data is changed until it is fixed; Trino applications are still registered.
Excluded catalogs are only hidden from the relation data: a consumer can still query them.

### Caveats

- Catalogs are advertised per consumer, but the single routing group sends each query to any
  registered Trino application. If the applications do not have the same catalogs, or one has
  a confirmation gap, some queries fail. Relate only Trino applications with the same catalogs.
- Catalogs with the same name on different Trino applications are treated as the same data.
  Only their connectors are compared, and a catalog without a known connector (no `backend`
  entry in `catalog-config`) never conflicts. A connector conflict holds back a new
  application rather than changing what published consumers see.
- Over plain HTTP, Trino trusts the username and ignores the password, and consumers such as
  Superset and DataHub leave the password out unless the port is `443`. The credentials only
  take effect once both the gateway and Trino are reached over HTTPS; mixing HTTP and HTTPS
  fails.
- In cross-model setups, each Trino application must publish an address the gateway can reach,
  and the gateway publishes its in-cluster address, which only consumers in the same
  Kubernetes cluster can reach.
- Operators must not configure users whose names start with `gw-` on a related Trino
  application: Trino refuses the gateway's user of the same name.

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
- The charm owns the whole backend list and removes backends that it has not admitted from
  `backend-trino-catalog` and `trino-gateway-integration` relations.
- All backends are in the single `adhoc` routing group, with no routing rules. See the
  [caveats](#caveats) for consumers.
- Gateway and Trino worker applications cannot be related.
- If the `postgresql` integration is removed, the unit becomes Blocked but the gateway keeps
  running, so clients get errors from the gateway rather than refused connections.

## Other resources

- [Charmhub](https://charmhub.io/trino-gateway-k8s)
- [Contributing](CONTRIBUTING.md)
- [Juju documentation](https://documentation.ubuntu.com/juju/3.6/howto/manage-charms/)
