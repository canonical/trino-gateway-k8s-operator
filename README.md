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

For a Trino application in another model, each side offers one endpoint. The Trino application
offers `trino-catalog`, which the gateway's model consumes:

```shell
juju offer trino-model.trino-k8s:trino-catalog trino-k8s
juju consume -m gateway-model trino-model.trino-k8s trino-a
juju integrate -m gateway-model trino-gateway-k8s:backend-trino-catalog trino-a:trino-catalog
```

The gateway offers `trino-gateway-integration`, which the Trino application's model consumes:

```shell
juju offer gateway-model.trino-gateway-k8s:trino-gateway-integration trino-gateway
juju consume -m trino-model gateway-model.trino-gateway trino-gateway
juju integrate -m trino-model trino-k8s:trino-gateway-integration trino-gateway:trino-gateway-integration
```

For a Trino application on another controller, prefix the offer paths and models with the
controller name, for example `juju consume -m trino-controller:trino-model
gateway-controller:admin/gateway-model.trino-gateway trino-gateway`.

The other direction, where the gateway's model consumes `trino-gateway-integration` from the
Trino application's model, is not supported. Juju does not let the gateway share its consumer
secrets with an application consumed from an offer, so once a consumer exists every gateway
hook fails with `sharing consumer secrets across a cross model relation not supported`.

Across controllers, the Trino application's pods read the consumer secrets from the gateway
model's secret backend, so they must be able to reach it. Otherwise the Trino application never
confirms the users, and consumers stay `waiting for publication`. For example, the default
secret backend of a MicroK8s model uses the API server address `127.0.0.1:16443`, which pods
cannot reach. A Kubernetes secret backend whose endpoint the pods can reach works, as
described in [Manage secret backends](https://documentation.ubuntu.com/juju/3.6/howto/manage-secret-backends/).
Set the gateway model's `secret-backend` before relating any consumer: changing it moves the
model's existing secrets to the new backend.

The two relations are matched by the address the Trino application publishes on both, not by
application name. The
charm keeps the gateway's registered backends identical to the related applications it
admits, as described in [Connect consumers](#connect-consumers). Backends added, changed or
removed through the gateway's API are reverted. All backends are
placed in the `adhoc` routing group and checked with the gateway's default unauthenticated
health check (`/v1/info`). With no related Trino applications, the unit reports
`no backends related`. Changes are applied by the leader unit on its next hook. After the gateway
restarts, the backend list may only converge on the following `update-status` hook.

- The backend is named after the related application, which for an offer is the name given to
  `juju consume` in the gateway's model, such as `trino-a` above. A name longer than 63 characters is shortened and given a hash suffix. If a name is still not a valid backend name, the relation adds no backend and
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

- the gateway's address: its ingress address when it has one, as described in
  [Expose the gateway through ingress](#expose-the-gateway-through-ingress), otherwise its
  in-cluster address, `trino-gateway-k8s.<model>.svc.cluster.local:8080`;
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
- A registered Trino application whose catalogs come to conflict, such as after a change to
  its `catalog-config`, stays registered and keeps receiving queries. Queries routed to it that
  use a conflicting catalog run against its own connector, so they fail or read other data.
- For a conflicting catalog, consumers are given the catalog of the registered application
  whose name sorts first among those that give it a connector. That can be the application
  that introduced the conflict.
- A consumer must publish its `app_name`, as consumers using the `trino_catalog` library do.

The leader unit reports progress in its status:

| Status | Meaning |
| --- | --- |
| Active `N upstreams waiting for registration` | Trino applications that are not registered yet. The logs say why. |
| Active `N consumers waiting for publication` | Consumers that have not been published yet. |
| Active `N confirmation gaps` | Registered applications that have not confirmed a published consumer. |
| Blocked `N users refused by M upstreams: <reasons>` | A Trino application refused users. Each reason is an operator error, such as a `gw-*` user configured on Trino, or a bug. |
| Blocked `N catalog conflicts between upstreams` | Catalogs with the same name use different connectors on different applications. A new application is held back, but a registered one keeps receiving queries, and those that use the catalog fail. Align their `catalog-config` promptly. |
| Blocked `invalid catalog-exclusions: <error>` | The option is not valid YAML. |

Blocked does not mean the gateway is down: it keeps routing queries to every registered
application. Whether consumers that already work keep working depends on the cause: a catalog
conflict on a registered application makes some of their queries fail.

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
  application, but does not take a registered application out of service, as described in
  [Connect consumers](#connect-consumers).
- Over plain HTTP, Trino trusts the username and ignores the password, and consumers such as
  Superset and DataHub leave the password out unless the port is `443`. The credentials only
  take effect when consumers reach the gateway through an HTTPS ingress, as described in
  [Expose the gateway through ingress](#expose-the-gateway-through-ingress).
- In cross-model setups, each Trino application must publish an address the gateway can reach.
  Without ingress, the gateway publishes its in-cluster address, which only consumers in the
  same Kubernetes cluster can reach.
- Operators must not configure users whose names start with `gw-` on a related Trino
  application: Trino refuses the gateway's user of the same name.

## Expose the gateway through ingress

The charm requires an optional `ingress` relation, using the `ingress` interface, to make the
gateway reachable from outside the Kubernetes cluster. The ingress provider terminates TLS;
the gateway keeps listening on plain HTTP on port `8080`.

The ingress address must be host-based, such as `https://trino-gateway.example.com`: Trino
clients and the `trino-catalog` interface cannot use a path prefix. Use a provider that gives
each application its own host name, such as `traefik-k8s` with `routing_mode=subdomain`, or
`gateway-api-integrator`:

```shell
juju deploy traefik-k8s --trust --config routing_mode=subdomain --config external_hostname=<domain>
juju integrate trino-gateway-k8s:ingress traefik-k8s:ingress
```

With traefik in `subdomain` mode, the gateway is served at
`<scheme>://<model>-trino-gateway-k8s.<domain>`, and the `ingress` relation data in
`juju show-unit trino-gateway-k8s/0` shows the address. Traefik serves HTTPS once it is related
to a TLS certificates provider.

Once the provider gives a host-based address:

- Trino clients, the web UI at `/trino-gateway` and the REST API are reachable at the ingress
  address. The in-cluster address on port `8080` keeps working.
- Every published consumer is given the ingress host and port as `trino_url`, such as
  `trino-gateway.example.com:443`, using the default port of the scheme when the address has
  none. When the address changes or the relation is removed, consumers are given the new
  address or the in-cluster address again.

Until the provider gives an address, consumers keep the in-cluster address and the status does
not change. If the address is path-based, the unit is Blocked with
`ingress URL <url> is path-based; a host-based ingress is required` and consumers keep the
in-cluster address; switch the provider to host-based addresses to resolve it.

The gateway processes the `X-Forwarded-*` headers set by the ingress and passes the client's
scheme, host and port on to the Trino clusters, so the follow-up URLs (`nextUri`) clients
receive point at the ingress address.

Through an HTTPS ingress, Trino receives the forwarded HTTPS protocol, so it accepts password
authentication: consumers given port `443` send their issued credentials, and they take
effect. Through an HTTP ingress or the in-cluster address, Trino trusts the username only.

> [!WARNING]
> Without an `oauth` relation, the gateway's web UI and REST API are not authenticated.
> Relating ingress exposes them, including the editing of the gateway's state, to everyone who
> can reach the ingress address. To require a login, relate an OAuth provider, as described in
> [Authenticate the web UI and REST API](#authenticate-the-web-ui-and-rest-api).

## Authenticate the web UI and REST API

The charm requires an optional `oauth` relation, using the `oauth` interface, to make users log
in to the gateway's web UI and REST API through an OAuth provider, such as the Canonical
Identity Platform:

```shell
juju integrate trino-gateway-k8s:oauth <oauth-provider>:oauth
```

OAuth needs an HTTPS ingress address, as described in
[Expose the gateway through ingress](#expose-the-gateway-through-ingress): the login sets a
session cookie that browsers only send over HTTPS. Once the gateway has one, the charm
registers an OAuth client whose redirect URI is the gateway's callback at that address, such as
`https://trino-gateway.example.com/oidc/callback`, and configures the gateway with the client.
When the address changes, the charm updates the client.

| Status | Meaning |
| --- | --- |
| Waiting `waiting for ingress URL for OAuth` | Ingress is not related, has no address yet, or its address is path-based. |
| Blocked `OAuth requires an HTTPS ingress URL` | The ingress address is HTTP. With traefik, relate it to a TLS certificates provider. |
| Blocked `invalid OAuth client configuration: <error>` | The OAuth library rejects the redirect URI built from the ingress address. |
| Waiting `waiting for OAuth provider registration` | The provider has not registered the client yet. |

While the relation exists but OAuth is not configured, the gateway keeps serving queries and
the charm keeps managing backends, but no user can log in to the web UI or REST API.

Once OAuth is configured:

- The web UI at `/trino-gateway` shows a login button that sends users to the provider.
  Requests to the web UI's data and the REST API without a login are refused with HTTP `403`.
- Every user who logs in gets read-only access to the web UI, including the query history of
  queries run under their user ID, the `sub` claim of their ID token. No user can edit backends
  or routing rules, or call the REST API under `/gateway`. The gateway answers such requests
  with HTTP `200` and `"code": 401` in the body.
- The charm calls the REST API to manage backends as the gateway user `juju:trino-gateway-k8s`,
  with short-lived tokens signed with a key in a Juju secret owned by the application. The
  gateway tries OAuth first, so it logs `Could not validate token or get claims from it` for
  each of these calls.
- A new client secret from the provider is applied automatically by restarting the gateway.

Trino queries are not authenticated by the gateway: it passes them, with their credentials, to
the Trino applications, which authenticate them as before. The gateway's own login uses the
`/oidc/callback` path; requests to `/oauth2/*` are still forwarded to the Trino applications.
Logging in to Trino through the gateway, such as with `trino --external-authentication`, is not
supported.

The gateway calls the provider's token and key endpoints itself and trusts only the
certificate authorities of Ubuntu's `ca-certificates` package. The provider's endpoints must
use a certificate from one of them; the charm ignores any CA certificate the provider sends,
so logins fail with a provider that uses a self-signed or private certificate authority.

When the `oauth` relation is removed, the web UI and REST API return to unauthenticated access.

## Connect a client

Clients connect to the gateway at its ingress address, as described in
[Expose the gateway through ingress](#expose-the-gateway-through-ingress), or, inside the
Kubernetes cluster, to its Service over HTTP, using their usual Trino credentials:

```text
http://trino-gateway-k8s.<model>.svc.cluster.local:8080
```

The gateway passes credentials through to the Trino cluster unchanged. Each Trino cluster must
be configured to work behind a proxy:

- `http-server.process-forwarded=true`, so that follow-up URLs point at the gateway rather than
  the cluster.
- `http-server.authentication.allow-insecure-over-http=true`, so that clients can authenticate
  with a user name only over plain HTTP. Trino accepts passwords over HTTP only when the
  forwarded protocol is HTTPS, so password authentication through the gateway needs an HTTPS
  ingress.

The `trino-k8s` charm sets both.

The gateway also processes the `X-Forwarded-*` headers of in-cluster clients, so a client that
sends `X-Forwarded-Proto: https` to the in-cluster address makes Trino accept its password
over plain HTTP.

## Limitations

- Only a single unit is supported.
- The gateway has no TLS listener of its own: TLS is only available through an ingress
  provider that terminates it, and only host-based ingress addresses are supported.
- Without an `oauth` relation, the gateway's admin web UI and REST API are not authenticated.
  Anyone who can reach port `8080` or the ingress address can change the gateway's state,
  although the charm reverts backend changes.
- With OAuth, some endpoints stay readable without a login: `/api/public/backends`, which
  lists the names, URLs and states of the backends, and the metrics and JMX endpoints
  `/metrics` and `/v1/jmx`. Logging out of the web UI does not end the session: the browser
  keeps the ID token until it expires.
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
