# Contributing to the Trino Gateway rock

## Build

```shell
rockcraft pack
```

## Test

The integration tests deploy PostgreSQL and Trino with Juju, run the rock as a plain Kubernetes
Deployment, and send a query to Trino through the Gateway. Run `opcli` commands from the
repository root and `tox` commands from this directory.

Closest to CI, in a fresh virtual machine:

```shell
opcli artifacts build --rock trino-gateway
opcli spread run
```

`opcli spread run -- -list` shows the available jobs; the ones for this suite include
`trino_gateway_rock/tests/integration`.

Faster, against an existing Juju controller on Kubernetes, with `kubectl` configured for the
same cluster:

```shell
opcli artifacts build --rock trino-gateway
opcli artifacts push-images --missing-registry deploy
cd trino_gateway_rock
tox -e integration
```

Pass `-- --no-juju-teardown` to keep the model for debugging.

Check formatting and types with `tox -e fmt` and `tox -e lint`.

## Upgrade Trino Gateway

In `rockcraft.yaml`, update these together:

- `version`;
- the jar URL in the `trino-gateway` part's `source`;
- `source-checksum`, the SHA-256 of the new jar.
