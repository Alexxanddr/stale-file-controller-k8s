# Security

This alpha controller can delete discovered Pods across namespaces. Its
privileged diagnostic agent can inspect node processes and enter container
mount namespaces. Treat the image, namespace, maintainers and anybody able to
exec into the agent as part of your node administration trust boundary.

The controller does not read Secrets or mutate volumes. The agent does not
mount the runtime socket or receive a service-account token. Keep write/exec
access to its namespace restricted. Standard RBAC cannot limit dynamic Pod
deletions by labels; namespace-scoped deletion roles are an option for operators
maintaining a fixed set of client namespaces.

Do not disclose exploit details or credentials in public issues. Use the
repository's private vulnerability reporting feature when available, or contact
the maintainer at the address in the project commit metadata to
arrange a private report. Include a minimal reproduction with synthetic data.
There is no guaranteed support SLA; review and test changes before production.
