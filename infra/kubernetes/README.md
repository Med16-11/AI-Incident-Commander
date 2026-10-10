# Kubernetes

OpsMind control-plane resources and `dev`, `staging`, and `production` Kustomize overlays are in [`opsmind/`](opsmind/). Read the [deployment architecture](../../docs/deployment-architecture.md) for provisioning, secrets, rollout commands, scaling limits, and production requirements.

The older [`shopstack/`](shopstack/) bundle is a development demo and is not part of the production overlays. Its detailed runbook is in [`docs/shopstack.md`](../../docs/shopstack.md).

The legacy [`remediation-rbac.yaml`](remediation-rbac.yaml) remains for the ShopStack demo cluster. The OpsMind overlays carry their own namespaced least-privilege Role and RoleBinding for remediation; do not grant either ServiceAccount cluster-admin access.
