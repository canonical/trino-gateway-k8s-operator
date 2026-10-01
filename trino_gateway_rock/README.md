# Trino Gateway rock

An OCI image of [Trino Gateway](https://trinodb.github.io/trino-gateway/) 21 on a `bare` base. It
contains the Gateway jar and the OpenJDK 25 JRE.

## Runtime

- Entrypoint: Pebble, which starts the `trino-gateway` service as the `_daemon_` user.
- Configuration: `/etc/trino-gateway/config.yaml`. The image ships none, so mount one.
- Port: `8080`.
- Readiness endpoint: `GET /trino-gateway/readyz`.

## Configuration example

The Gateway stores backends and query history in PostgreSQL. `${ENV:...}` reads a value from an
environment variable, which keeps the password out of the file:

```yaml
serverConfig:
  node.environment: production
  http-server.http.port: 8080
dataStore:
  jdbcUrl: jdbc:postgresql://postgresql:5432/trino_gateway
  user: trino_gateway
  password: ${ENV:DB_PASSWORD}
  driver: org.postgresql.Driver
clusterStatsConfiguration:
  monitorType: INFO_API
```
