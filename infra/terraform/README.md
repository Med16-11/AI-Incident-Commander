# Terraform

The repository keeps cloud-provider selection external. The Terraform root configures Kubernetes `ResourceQuota` and `LimitRange` guardrails for the Kustomize-created namespace. Use the environment examples, tune quota values to cluster capacity, and configure remote encrypted state and cluster access before applying. Commit the generated `.terraform.lock.hcl` after reviewing provider checksums. See [`docs/deployment-architecture.md`](../../docs/deployment-architecture.md#terraform-boundary).

Cloud cluster, network, managed database/cache, secret-manager, and load-balancer resources should be provisioned through the operator's existing cloud modules. Keep secret values out of Terraform state and pass only references or runtime-injected values to OpsMind.
