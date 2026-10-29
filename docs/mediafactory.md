# Mediafactory example

This example illustrates configuring the generic controller for one Talos
homelab. It is not a required topology or a default for other users.
The actual deployment is maintained by the separate
[Mediafactory GitOps repository](https://github.com/Alexxanddr/mediafactory).

Set these values in the configuration ConfigMap of the generic base:

```yaml
STORAGE_NAMESPACE: mergerfs
MERGERFS_SELECTOR: app=mergerfs
NFS_SELECTOR: app=nfs-ganesha
MERGERFS_CONTAINER: mergerfs
NFS_CONTAINER: nfs-ganesha
MERGERFS_PATH: /mnt/merged/MediaFactory
NFS_BACKING_PATH: /export/MediaFactory
MERGERFS_HOST_PATH: /run/mergerfs
NFS_SERVER: 192.168.20.3
NFS_EXPORT_PATH: /MediaFactory
EXTRA_PATHS: '["torrents"]'
```

MergerFS propagates `/mnt/merged` to `/run/mergerfs` using Bidirectional mount
propagation. Ganesha receives it at `/export`. The only pseudo-export is
`/MediaFactory`, with clients `192.168.20.0/24`, `192.168.10.0/24` and
`192.168.99.0/24`. Do not alter these ACLs to accommodate diagnostics: the
host-network agent uses the node's existing allowed address. Keep Root_Squash.
The local MergerFS PV used by Plex is included alongside NFS CSI clients.
NFS CSI 4.13.4 handles publish/remount only after the controller's server gates.

The homelab overlay adds its own Keel and Uptime Kuma annotations. Those tools
are optional integrations and are deliberately absent from the reusable base.
All changes go through Flux; no direct apply is needed.

Read-only verification for this cluster:

```sh
KUBECONFIG=~/.kube/talos kubectl -n storage-recovery get pods
KUBECONFIG=~/.kube/talos kubectl -n storage-recovery logs deployment/storage-recovery -f
KUBECONFIG=~/.kube/talos kubectl -n storage-recovery get configmap storage-recovery -o jsonpath='{.data.state}'
```
