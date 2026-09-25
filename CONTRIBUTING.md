# Contributing

## Development setup

Install the development tooling and verify the machine setup:

```shell
opcli install all
opcli install doctor
```

`opcli spread run` creates LXD virtual machines. After adding your user to the `lxd` group, start a
new login session or run `newgrp lxd` before using Spread.

Keep local credentials and tokens in `.secrets.env`. The file is ignored by Git and must not be
committed.

## Fast feedback

Run the focused non-integration checks while developing:

```shell
tox -e fmt
tox -e lint
tox -e static
tox -e unit
```

## Integration testing

The preferred path, and the closest match to CI, is Spread:

```shell
opcli artifacts build
opcli spread run
```

Discover available Spread selectors with:

```shell
opcli spread run -- -list
```

On a machine that already runs MicroK8s or has port `32000` open, prefer Spread. The manual path's
registry autodetection can push images to that existing registry rather than the registry for the
newly provisioned environment.

For a faster manual local run, use this order:

```shell
opcli artifacts build
opcli env provision
opcli artifacts push-images --missing-registry deploy
opcli pytest run
```

Provisioning before `push-images` is required only for this manual path. Spread's generated setup
already provisions its isolated environment before it pushes images.

## Artifact contracts

`artifacts.yaml` declares the rock, charm, and their resource mapping. `opcli artifacts build`
generates `build/artifacts.build.yaml`, which records the built artifact paths. `concierge.yaml`
describes the Juju and Kubernetes environment used by `opcli env provision`, and `spread.yaml`
defines the isolated integration-test environment that runs the same workflow. The generated
`build/` directory is ignored by Git.

## Dependency updates

The Charm CI workflow SHA in `.github/workflows/` and the `opcli` pin in `pyproject.toml` and
`uv.lock` must move together. Renovate groups these updates because the Charm CI workflow derives
the compatible opcli version from its pinned SHA.
