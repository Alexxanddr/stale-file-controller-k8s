# Stale file recovery controller

Python controller for MergerFS + NFS-Ganesha on Kubernetes/Talos. It polls
infrastructure identities, restart counters, Ready status and **real filesystem
operations**. Initial startup deliberately performs a recovery to establish a
verified baseline. Polling is every 30 seconds when idle, 5 during recovery;
API failures use exponential backoff up to 60 seconds. Scans are serial and
large/unhealthy clusters take longer than one polling interval.

## Recovery contract

1. Restart `mergerfs/mergerfs`, or wait for an existing Pending, terminating or
   container restart. A running but unready filesystem is restarted.
2. Verify the container's actual FUSE mount and read `/mnt/merged/MediaFactory`.
3. Restart `mergerfs/nfs-ganesha` **after step 2** (or await its ongoing restart).
4. Check its FUSE backing path `/export/MediaFactory`, then mount the NFSv4.1
   export `/MediaFactory` read-only in an isolated diagnostic mount namespace
   and read it. This uses host networking so the node belongs to an allowed
   subnet. The export's Root_Squash and client CIDRs are preserved.
5. Snapshot and normally delete every discovered client, with UID preconditions.
   Wait for replacements to become Ready and verify their actual mounted paths.
   All clients are requested before waiting for readiness; one API failure does
   not prevent requests for the remaining clients.

A precreated Lease is renewed every 10 seconds, expires after 60 seconds, and
uses optimistic concurrency. Losing renewal terminates the controller. One
replica with Recreate plus the Lease prevents concurrent recovery workers.
A precreated ConfigMap persists phase, infrastructure identities, restart
intents and client identities; it contains **no pod specs or environment**.
Intents are saved before infrastructure deletion. UID fences make replay safe.
Flux seeds Lease/ConfigMap only if missing, and does not prune their state.
Concurrent infrastructure changes before their health gate are absorbed into
one cycle. Changes after a health gate invalidate downstream work and restart
at MergerFS. The controller rechecks both infrastructure layers before client
restarts and while awaiting verification. Expected controller-induced restarts
do not generate a second cycle.

## Actual client mount checks

Discovery follows Pod volume -> namespaced PVC -> bound PV, matching CSI
`nfs.csi.k8s.io`. It also supports inline CSI/NFS, legacy NFS PVs and volumes,
`hostPath` volumes and hostPath/local PVs under `/run/mergerfs` (including Plex).
All matching mount paths, sidecars and running init containers are included;
completed init containers have no live namespace to check. Terminal pods,
this controller's dedicated namespace and the two infrastructure pods are
excluded. Readiness of every current client is required after recovery.

A privileged hostPID DaemonSet has no service account token and no API access.
The controller can exec diagnostics only in its own namespace, never in client
pods. The agent finds a process using the full runtime container ID in cgroups,
checks `/proc/PID/mountinfo`, calls `setns()` into that mount namespace, and
`chroot()` into `/proc/PID/root`. Its already-loaded Python runtime performs
`stat`, `statvfs`, `scandir` and entry `stat` on the exact client mount, including
an explicit `torrents` child check. No client shell, `df`, `stat`, Python,
ephemeral container, runtime socket or host-installed utility is required.
Mount type must be NFS/NFS4 or FUSE as discovered. An unrelated mount cannot
make this check fail. Up to 128 entries are sampled; it is not a recursive
scan of the entire export. Absolute symlinks resolve inside the target root.

`ESTALE` or the exact diagnostic `Stale file handle` triggers recovery.
EACCES/EPERM are reported as permission problems and do not restart clients.
Timeout, EIO, transport disconnection and refused connections are temporary;
three consecutive unhealthy client scans trigger recovery. Other diagnostic
errors are logged for operator attention, not treated as proven stale handles.
The probe uses root locally; it checks storage health, not application UID ACLs.
NFS server-side root squashing still applies.

## Timeouts and blocked I/O

Each filesystem probe has a 20-second supervisor deadline; exec has a 30-second
deadline, API operations 10–15 seconds. Fresh export checks use a private,
read-only `soft,timeo=10,retrans=1` mount, never the application's mount options.
No write, unmount or remount is issued against client or kubelet mounts.
A timed-out probe is SIGKILLed without waiting. Kernel D-state I/O cannot be
killed until the server/kernel returns; a per-node flock and cap of four
outstanding workers prevents unbounded accumulation. Missing agents fail
closed. Restarting an agent does not repair uninterruptible kernel I/O.

Normal Pod deletion preserves grace periods and does not force detach. A pod
stuck Terminating blocks completion; after 15 minutes the controller logs an
explicit blocked warning and `/healthz` returns 503 for Uptime Kuma. It retains
the phase, retries with bounded requests and never escalates to forced deletion,
volume removal or node reboot. Restore storage connectivity, inspect pod events,
node I/O and CSI logs, and let kubelet finish teardown. Node intervention is an
operator decision. Do not delete the checkpoint during a running recovery.

Unmanaged bare/static pods cannot be recreated safely with delete-only RBAC;
they are reported and block completion. ReplicaSet/DaemonSet owner UID changes
(e.g. a simultaneous Deployment rollout), scale-down or suspended Jobs may need
operator review if the snapshotted replacements no longer exist. StatefulSets
are matched by owner UID and stable pod name. Recovery does not alter replicas,
Jobs, PVCs, PVs, exports or user data. A second stale mount after client restart
blocks validation and is reported; it is not hidden by another deletion loop.

## CSI 4.13.4

[Upstream NodePublishVolume](https://github.com/kubernetes-csi/csi-driver-nfs/blob/v4.13.4/pkg/nfs/nodeserver.go#L129-L141)
checks `lstat(targetPath)` on an existing mount, unmounts/remounts on ESTALE,
and returns success on permission errors. This is a NodePublish path, not a
continuous in-pod monitor. The controller relies on kubelet/CSI's normal new-pod
publish after both servers pass their gates; it does not invoke CSI directly
or restart the CSI DaemonSet. A successful CSI remount alone does not establish
that an already-bound client namespace is healthy.

## Permissions and deployment

- Cluster: list Pods, PVCs and PVs; delete Pods. Dynamic cross-namespace
  discovery needs cluster read permissions; dynamic Pod deletion cannot be
  restricted by a label selector in standard RBAC. The controller's code limits
  deletion to the infrastructure and discovered clients. Consider namespace
  RoleBindings instead if you maintain a fixed list of client namespaces.
- Only `storage-recovery`: get pods/exec (WebSocket GET), get/patch the named checkpoint,
  get/update the named Lease. No create Pods, Secrets, deployment edits,
  ephemeral containers, PVC/PV writes, node mutations or cluster exec rights.
- Agent: privileged root, hostPID and hostNetwork are required for namespace
  access and a real export mount. This is a powerful node trust boundary; only
  trusted administrators should be able to exec into its namespace. Controller
  itself runs non-root, read-only root filesystem, without capabilities.

`deploy/` is a standalone Kustomize package. The same manifests are integrated
in `mediafactory/flux/infrastructure/storage-recovery/`, recursively included
by its infrastructure Flux Kustomization. No CRDs are needed. The controller
works before the application layer exists and discovers later workloads.
`NFS_SERVER` defaults to the configured node endpoint; update it when moving
Ganesha. Only `/MediaFactory` is used by the fresh export check.

GitHub Actions tests and builds public GHCR images for amd64/arm64 using only
`GITHUB_TOKEN`; no registry secret is committed. `latest` is the moving Flux
image and a Git SHA image is also published. Confirm the GHCR package is public
before merging the GitOps deployment. Pushing the software repo builds an image;
merging the infrastructure branch activates reconciliation. No live cluster
resources are applied by this project's tooling.

Read-only checks after deployment:

```sh
KUBECONFIG=~/.kube/talos kubectl -n storage-recovery get pods
KUBECONFIG=~/.kube/talos kubectl -n storage-recovery logs deployment/storage-recovery -f
KUBECONFIG=~/.kube/talos kubectl -n storage-recovery get configmap storage-recovery -o jsonpath='{.data.state}'
KUBECONFIG=~/.kube/talos kubectl -n storage-recovery get lease storage-recovery
```

In a maintenance window, restart either infrastructure workload and inspect
phase order and client mount results; repeat with both restarts close together.
Expect one completed cycle, with a necessary subsequent Ganesha restart after
MergerFS verification. Fault injection is not performed automatically on a
production cluster. Also test a blocked NFS call and a shell-less client in a
staging Linux cluster before enabling unattended production recovery.

## Development

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m unittest discover -s tests -v
KUBECONFIG=~/.kube/talos kubectl kustomize deploy >/tmp/storage-recovery.yaml
docker build -t stale-file-controller:test .
```

The Linux Docker smoke test creates a real FUSE mount in a client with an empty
root (no shell or diagnostic utilities), and checks it from a separate hostPID
agent. Run `sh tests/linux/smoke.sh` after building the image.

Unit tests exercise discovery, errno classification, mount selection, staged
recovery ordering, replay/deduplication and regression during recovery. They do
not prove kernel mount propagation or NFS teardown on Talos; those require the
staging checks above. Health is a machine-only endpoint with no HTTPRoute.
