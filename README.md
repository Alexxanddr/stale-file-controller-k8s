# stale-file-controller-k8s

[![CI](https://github.com/Alexxanddr/stale-file-controller-k8s/actions/workflows/ci.yaml/badge.svg)](https://github.com/Alexxanddr/stale-file-controller-k8s/actions/workflows/ci.yaml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

A Kubernetes controller that coordinates recovery of a **MergerFS → NFS-Ganesha
→ client Pods** storage chain. It detects infrastructure restarts and real
filesystem failures, then verifies every layer before restarting the next one.
It checks mounts inside client namespaces, including clients without a shell.

This project is alpha. It targets Linux Kubernetes clusters using NFSv4.1 and a
single MergerFS/Ganesha pair per controller installation. It does not provision
storage, deploy the servers, or manage their export configuration. Topology,
selectors, endpoints and paths are configurable; no particular homelab is
required. Images are published for `linux/amd64` and `linux/arm64`:

```text
ghcr.io/alexxanddr/stale-file-controller-k8s:latest
ghcr.io/alexxanddr/stale-file-controller-k8s:<git-sha>
```

## Recovery behavior

```text
MergerFS restart (or await an ongoing restart)
  → verify FUSE + read the configured union directory
    → NFS-Ganesha restart (or await its ongoing restart)
      → verify backing mount + read the real NFS export
        → restart discovered clients
          → wait for replacements Ready + verify their actual mounts
```

The first startup performs this cycle to establish a verified baseline, so
activation briefly interrupts storage workloads. A running but unready server
is restarted; Pending, terminating or actively restarting containers are awaited.
Ganesha is always processed after MergerFS passes its filesystem check, even if
both servers originally restarted together.

A single-replica Recreate Deployment, a resourceVersion-fenced Lease, and a
persisted ConfigMap checkpoint serialize recovery and support safe replay.
The Lease renews every 10 seconds and expires after 60; renewal failure stops
the process. Infrastructure restart intents are saved before deletion. Pod UID
preconditions prevent replay from deleting a replacement. Concurrent changes
before a health gate are absorbed; changes after a health gate invalidate
downstream work. Changing configuration also invalidates an in-flight cycle.

## Installation

Requirements:

- Linux nodes, a compatible container runtime exposing full container IDs in
  `/proc/PID/cgroup`, Kubernetes Lease and exec APIs;
- existing MergerFS and NFS-Ganesha Pods managed by a workload controller;
- FUSE propagated to the host and Ganesha, existing NFSv4.1 exports, and network
  access from every diagnostic node to the configured server;
- permission to install the RBAC and a privileged hostPID/hostNetwork DaemonSet.

The base [deploy/](deploy/) is a Kustomize package with example values. **Configure
it before activation.** Set `storage-recovery-config` to your actual topology;
selectors must each match exactly one infrastructure Pod. Start from
[examples/custom-storage](examples/custom-storage/kustomization.yaml) or vendor
the base into your GitOps repository. Render the result for review:

```sh
KUBECONFIG=/path/to/kubeconfig kubectl kustomize examples/custom-storage > /tmp/recovery.yaml
```

Reconcile the rendered manifests through your normal deployment workflow, such
as a Flux Kustomization pointing at your vendored overlay. The base has no
Flux-specific dependency, CRD, registry credential, HTTPRoute or public service.
The included Service exposes only health on port 8080. Prefer a SHA image tag
for controlled upgrades; `latest` follows master. Update both the controller
and agent images together. Recreate the controller Pod after ConfigMap changes
so it reloads configuration (or use your existing ConfigMap rollout automation).

The checkpoint ConfigMap and Lease use Flux `IfNotPresent`/`prune: Disabled`
annotations. Keep those objects runtime-owned in other GitOps tools as well;
continually overwriting their contents loses recovery state. Do not rename
these objects or apply a global namePrefix without updating the named RBAC
resources and controller state identifiers. This release supports one topology
per installation; multiple installations need separate namespaces and unique
cluster-scoped RBAC names.

## Configuration

The installed `stale-file-controller --help` and `--version` commands work
without Kubernetes access. `NFS_SERVER=nfs.example.test stale-file-controller
--check-config` validates and prints configuration without contacting a cluster.

Environment variables are loaded from `storage-recovery-config`; they can also
be set directly on the Deployment. No values are secrets. The controller uses
its in-cluster service account, not a kubeconfig file.

| Variable | Default | Meaning |
|---|---|---|
| `POD_NAMESPACE` | downward API | Controller, agent, checkpoint and Lease namespace |
| `STORAGE_NAMESPACE` | `storage-system` | Default infrastructure namespace |
| `MERGERFS_NAMESPACE` / `NFS_NAMESPACE` | empty | Per-server namespace override |
| `MERGERFS_SELECTOR` | `app=mergerfs` | Kubernetes label selector for the union server |
| `NFS_SELECTOR` | `app=nfs-ganesha` | Label selector for the export server |
| `MERGERFS_CONTAINER` | `mergerfs` | Container to inspect in the union Pod |
| `NFS_CONTAINER` | `nfs-ganesha` | Container to inspect in the export Pod |
| `MERGERFS_PATH` | `/mnt/merged/data` | Directory below the union's FUSE mount |
| `NFS_BACKING_PATH` | `/export/data` | Export server's backing FUSE directory |
| `MERGERFS_HOST_PATH` | `/run/mergerfs` | Host root used for direct-client discovery |
| `NFS_SERVER` | required | Host/IP reachable from diagnostic agents; base has example DNS |
| `NFS_EXPORT_PATH` | `/data` | NFSv4 pseudo-path to mount and read |
| `EXTRA_PATHS` | `[]` | JSON array of optional relative paths checked inside each mount |
| `IDLE_INTERVAL` | `30` | Seconds between healthy scans |
| `RECOVERY_INTERVAL` | `5` | Seconds between recovery iterations |
| `TRANSIENT_THRESHOLD` | `3` | Consecutive temporary client-failure scans before recovery |
| `BLOCKED_AFTER` | `900` | Seconds before blocked-recovery warning and unhealthy HTTP status |

Paths must be normalized absolute non-root paths. Extra paths cannot escape
the mount. An optional extra path absent on a particular mount is skipped;
ESTALE or access errors on an existing path are preserved. For example, set
`EXTRA_PATHS='["torrents"]'` to detect
`df: /export/MediaFactory/torrents: Stale file handle` on the affected client
mount without running global `df -h`.

## Discovery and real filesystem checks

The controller follows **Pod → namespaced PVC → bound PV** and recognizes
`nfs.csi.k8s.io`. It also supports inline CSI/NFS, legacy NFS PVs, hostPath
volumes, and hostPath/local PVs below `MERGERFS_HOST_PATH`. It inspects every
matching mount in regular containers and running init containers, including
subPath mounts at their actual container path. Completed init containers have
no live namespace to inspect. Terminal Pods, the selected infrastructure Pods,
and the controller/agent Pods are excluded.

A privileged node agent identifies a process by its full runtime container ID,
checks its mountinfo, opens its root, enters its mount namespace, and chroots
through the saved descriptor. Its already-loaded Python runtime performs
`stat`, `statvfs`, directory reads and entry `stat` at the exact client path.
No client shell, diagnostic utility, runtime socket, ephemeral container or
host-installed binary is needed. The expected mount type must be NFS/NFS4 or
FUSE. An unrelated mount cannot make the target check fail.

The NFS server gate also creates a **new read-only NFSv4.1 mount** in a private
namespace and reads its configured export. A Ready Pod or TCP 2049 alone is
never enough. Diagnostic mounts use `soft,timeo=10,retrans=1`; client mount
options, export ACLs, root squashing and kubelet mounts remain unchanged.

ESTALE triggers recovery. EACCES/EPERM are reported as permissions problems;
they do not trigger client restarts. Timeouts, EIO, disconnections and connection
failures are temporary and use the configured consecutive-scan threshold.
Other diagnostic errors require operator attention. Checks use root locally
and still obey NFS server-side root squashing; they are not application-UID ACL
tests. Up to 128 directory entries and the explicit extra paths are sampled,
not the entire directory tree.

## Safety and limits

- **No volume or data deletion.** Only normal Pod deletion with UID
  preconditions; no force-delete, grace-period override, force detach, node
  reboot, kubelet unmount or client remount command.
- **Bounded diagnostics.** Each filesystem worker has a 20-second deadline;
  exec has 30 seconds and API calls 10–15 seconds with transport retries
  disabled. API errors use exponential backoff up to 60 seconds. Scans are
  serial, so unhealthy/large clusters take longer than one polling interval.
- **Blocked kernel I/O.** A timed-out worker is SIGKILLed without waiting, but
  uninterruptible D-state I/O can survive until the kernel/server returns.
  A per-node flock and limit of four outstanding workers prevent unbounded
  accumulation. Missing agents fail verification.
- **Terminating clients.** Recovery waits for normal teardown. After
  `BLOCKED_AFTER`, it logs an explicit warning and `/healthz` returns 503.
  It retains its checkpoint and does not escalate. Restore storage connectivity
  and inspect Pod events, node I/O and CSI logs. Node intervention is manual.
- **Managed workloads.** Bare/static Pods cannot be safely recreated with
  delete-only RBAC and block completion. ReplicaSet/DaemonSet owner changes,
  concurrent scale-down or completed/suspended Jobs may need operator review.
  StatefulSet replacements are matched by stable name and owner UID. A stale
  replacement blocks completion without a repeated client deletion loop.

The agent needs privileged root, hostPID and hostNetwork. It has no API token.
The controller runs non-root with a read-only root filesystem and no Linux
capabilities. RBAC grants cluster-wide **list Pods/PVCs/PVs and delete Pods**,
exec WebSocket GET only in its namespace, get/patch only its named checkpoint,
and get/update only its named Lease. No Secret access, Pod creation, deployment
edits, ephemeral containers or volume writes. Standard RBAC cannot restrict
dynamic deletion by label; fixed-namespace installations can replace that
permission with namespace RoleBindings. See [SECURITY.md](SECURITY.md).

## NFS CSI behavior

[NFS CSI v4.13.4 NodePublishVolume](https://github.com/kubernetes-csi/csi-driver-nfs/blob/v4.13.4/pkg/nfs/nodeserver.go#L129-L141)
checks an existing mount for ESTALE and attempts a normal unmount/remount during
publish. It is not a continuous client-namespace monitor. This controller
lets kubelet/CSI perform normal teardown and publish after the storage gates,
then verifies the resulting client mounts. It does not restart CSI or invoke
its RPCs. Other CSI versions must be evaluated by their maintainers/operators;
this project does not patch CSI behavior.

## Verification and development

```sh
KUBECONFIG=/path/to/kubeconfig kubectl -n storage-recovery get pods
KUBECONFIG=/path/to/kubeconfig kubectl -n storage-recovery logs deployment/storage-recovery -f
KUBECONFIG=/path/to/kubeconfig kubectl -n storage-recovery get configmap storage-recovery -o jsonpath='{.data.state}'
```

Test a server restart, concurrent server restarts, an inaccessible export,
permission errors and a shell-less client in a staging Linux cluster before
unattended production recovery. Confirm phase order, a single completed cycle,
and verified replacement mounts. Unit tests do not prove Talos/kernel mount
propagation or NFS teardown with blocked I/O.

See [CONTRIBUTING.md](CONTRIBUTING.md) for unit and Docker integration tests.
The Linux smoke test uses a real FUSE mount, a shell-less/chrooted client, and a
separate hostPID diagnostic agent. CI runs these checks before publishing
multiarch images. The [Mediafactory deployment example](docs/mediafactory.md)
is an operator-specific configuration, separate from the reusable base.
