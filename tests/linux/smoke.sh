#!/bin/sh
# Local Docker/CI only; no Kubernetes, no host paths, no network for probes.
set -eu
image=${1:-stale-file-controller:test}
target_image=stale-file-controller-fuse-test
container=stale-fuse-test-$$
trap 'docker rm -f "$container" >/dev/null 2>&1 || true' EXIT
# This force cleanup is exclusively for disposable local Docker fixtures.
docker build --build-arg "BASE_IMAGE=$image" -t "$target_image" tests/linux
cid=$(docker run -d --name "$container" --privileged --cgroupns host --network none "$target_image")
attempt=0
until docker logs "$container" 2>&1 | grep -q READY; do
    attempt=$((attempt + 1))
    if [ "$attempt" -ge 60 ]; then
        docker logs "$container"
        exit 1
    fi
    sleep 1
done
result=$(docker run --rm --privileged --pid host --cgroupns host --network none --read-only --tmpfs /tmp \
    "$image" python -m stale.probe \
    "{\"mode\":\"container\",\"containerID\":\"containerd://$cid\",\"path\":\"/mnt/merged/MediaFactory\",\"kind\":\"fuse\"}")
printf '%s\n' "$result"
printf '%s\n' "$result" | python3 -c 'import json,sys; assert json.load(sys.stdin)["status"] == "ok"'
result=$(docker run --rm --privileged --pid host --cgroupns host --network none --read-only --tmpfs /tmp \
    "$image" python -m stale.probe \
    "{\"mode\":\"container\",\"containerID\":\"containerd://$cid\",\"path\":\"/unmounted\",\"kind\":\"nfs\"}")
printf '%s\n' "$result" | python3 -c 'import json,sys; assert json.load(sys.stdin)["status"] == "temporary"'
echo 'Linux namespace/FUSE/shell-less smoke test passed'
