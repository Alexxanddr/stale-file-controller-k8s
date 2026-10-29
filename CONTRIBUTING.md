# Contributing

Issues and pull requests are welcome. Include your Kubernetes/runtime versions,
redacted storage topology, relevant phase logs and expected behavior. Never post
kubeconfigs, credentials, private hostnames or production pod specs.

Use Python 3.11+:

```sh
python3 -m venv .venv
.venv/bin/pip install -e .
.venv/bin/python -m unittest discover -s tests -v
docker build -t stale-file-controller:test .
sh tests/linux/smoke.sh
```

The Docker smoke test needs a local Linux Docker engine with privileged
containers and FUSE support. It creates disposable fixtures without host-path
mounts or network access for probes; it does not contact Kubernetes. Its fixture
cleanup uses Docker force removal, never Kubernetes force deletion.

Keep topology-specific settings in examples/overlays. Preserve recovery order,
UID preconditions, fail-closed verification, Lease ownership and replayable
checkpoints. Add behavioral tests for changes in these guarantees. Describe
kernel/runtime limitations explicitly; unit tests cannot prove production NFS
teardown. Production fault injection is never part of CI.

CI runs unit and Linux namespace tests, then publishes amd64/arm64 images on
master pushes. SHA tags identify a build; `latest` tracks master. No publishing
credentials are needed in a contributor's checkout.
