# Kubernetes deployment architecture

OpsMind uses Kustomize for application resources and environment overlays. This keeps one typed deployment base while allowing reviewed `dev`, `staging`, and `production` differences. Helm is not added because Kustomize is already used by the repository and the release currently has no chart dependencies.

## Workload layout

Each environment runs in its own namespace: `opsmind-dev`, `opsmind-staging`, or `opsmind-production`. The API, incident engine, detection engine, remediation engine, notification service, worker, Go event-ingestion service, and web console each have a ClusterIP Service and Deployment. An ingress controller or gateway is platform-owned; none of the app Services is exposed as a public LoadBalancer.

The remediation Deployment uses its own ServiceAccount in the OpsMind namespace. The deploy script installs a Role and RoleBinding into the separately configured monitored-workload namespace, granting deployment get/patch, deployment scale patch, ReplicaSet list, pod list, pod log get, and event list. Use a dedicated monitored-workload namespace: these permissions apply to every matching resource in that namespace. All other workloads disable service account token mounting. Workloads use non-root identities, seccomp RuntimeDefault, read-only root filesystems where supported, dropped capabilities, resource requests/limits, startup/liveness/readiness probes, and PodDisruptionBudgets.

The Python workloads share one OpsMind image with per-service Uvicorn commands. Go event ingestion is a separate minimal image. A release-specific Alembic Job migrates PostgreSQL before the deploy script waits for rollout completion. Supply immutable image references and a unique release identifier for each rollout.

## Environment responsibilities

| Environment | Namespaces and dependencies | Capacity and operation |
|---|---|---|
| `dev` | Single `opsmind-dev` namespace. Overlay includes single-replica PostgreSQL, Redis, Prometheus, Loki, OTEL Collector, and Jaeger for cluster-local development. | One replica for application workloads, small HPA ceilings, debug logging. The PostgreSQL PVC requires a StorageClass. These dependencies are disposable development defaults. |
| `staging` | `opsmind-staging`. PostgreSQL, Redis, Prometheus, Loki, and OTLP endpoints come from staging infrastructure and the runtime Secret. | At least two replicas for scalable services; topology spread prefers distinct nodes. Separate endpoint CIDRs and credentials from production. |
| `production` | `opsmind-production`. Use managed HA PostgreSQL and Redis plus managed or independently operated metrics, logs, and tracing backends. | API starts at three replicas; other scalable services at two. HPA ceilings are higher, Pods spread across zones, and Pod Security Admission enforces `restricted`. |

The detection engine remains one replica in every overlay because it runs a polling loop without distributed leader election. Its idempotency store reduces duplicate events but does not make duplicate pollers desirable. Add leader election or partitioned polling before raising this replica count.

HPA resources use CPU and memory utilization and require a working Kubernetes Metrics Server. Detection is intentionally not autoscaled. PDBs require one available Pod; the detection singleton therefore blocks voluntary disruption while unavailable, by design.

## Configuration and secrets

Non-secret tuning lives in `base/configmap.yaml` and environment overlay patches. Database URLs, Redis URLs, the event-ingestion bearer token, metrics/logging/tracing endpoints, internal trigger tokens, LLM credentials, and Slack credentials belong in the `opsmind-runtime` Secret. No populated Secret or `.env` file is committed. Each Pod template carries the unique release ID so a configuration or credential change paired with a new release ID restarts the workloads.

For local development, copy the relevant `secrets.env.example` to the ignored `secrets.env`, set a unique database password and update its URL using URL-encoded credentials. The deploy script creates or updates the Secret from that file. For staging and production, prefer an External Secrets controller or the cluster's supported secret-manager integration to create `opsmind-runtime`; do not leave production credentials in a workstation file. Set the matching database/Redis/observability endpoint CIDRs in the deployment environment. Set `OPSMIND_TARGET_NAMESPACE` to the existing, dedicated namespace containing the workloads OpsMind may inspect or remediate, and set `OPSMIND_KUBERNETES_API_CIDR` plus `OPSMIND_KUBERNETES_API_PORT` to the cluster API endpoint. Network policies default deny ingress/egress, permit required namespace traffic and DNS, allow HTTPS from incident, notification, and web Pods (LLM, Slack, and Prometheus proxy use), and allow managed services only on their declared ports and endpoint CIDRs. The standard NetworkPolicy API cannot restrict HTTPS to hostnames; use an egress proxy or CNI FQDN policy for destination allowlisting.

`OPSMIND_SLACK_REMEDIATION_API_URL` must use HTTPS in staging and production. The development overlay uses an in-namespace HTTP endpoint. Production database URLs should require TLS (for PostgreSQL, include the provider's TLS mode); Redis URLs should use `rediss://` when supported.

## Deployment steps

1. Provision a Kubernetes cluster, CNI with NetworkPolicy enforcement, Metrics Server, StorageClass for dev, ingress/TLS, image registry access, managed data services for staging/production, and a secret-manager integration.
2. Copy the desired quota example, create the environment namespace, and apply Terraform guardrails before scheduling workloads:

   ```sh
   cp infra/terraform/environments/production.tfvars.example infra/terraform/production.tfvars
   kubectl apply -f infra/kubernetes/opsmind/overlays/production/namespace.yaml
   cd infra/terraform
   terraform init
   terraform apply -var-file=production.tfvars
   cd ../..
   ```

   Substitute the desired environment's namespace and tfvars file. Repeat per environment.
3. Build and push immutable application, web, and Go event-ingestion images:

   ```sh
   docker build -f infra/docker/Dockerfile -t "$OPSMIND_IMAGE" .
   docker push "$OPSMIND_IMAGE"
   docker build -f infra/docker/web.Dockerfile -t "$OPSMIND_WEB_IMAGE" .
   docker push "$OPSMIND_WEB_IMAGE"
   docker build -f apps/event-ingestion/Dockerfile -t "$OPSMIND_EVENT_INGESTION_IMAGE" .
   docker push "$OPSMIND_EVENT_INGESTION_IMAGE"
   ```

4. For dev, copy `infra/kubernetes/opsmind/overlays/dev/secrets.env.example` to `secrets.env`, replace every `CHANGE_ME`, and keep it out of source control. For staging/production, create the `opsmind-runtime` Secret from the environment's secret manager before deploying.
5. Set `OPSMIND_IMAGE`, `OPSMIND_WEB_IMAGE`, and `OPSMIND_EVENT_INGESTION_IMAGE` to immutable registry references, `OPSMIND_RELEASE_ID` to a unique lowercase release ID, `OPSMIND_TARGET_NAMESPACE` to the existing dedicated monitored-workload namespace, and `OPSMIND_KUBERNETES_API_CIDR` plus `OPSMIND_KUBERNETES_API_PORT` to the API endpoint. Staging/production also require `OPSMIND_DB_CIDR`, `OPSMIND_REDIS_CIDR`, `OPSMIND_OBSERVABILITY_CIDR`, and `OPSMIND_METRICS_CIDR`. Use the narrow endpoint/source ranges required by the cluster network.
6. Deploy from the repository root:

   ```sh
   scripts/deploy-k8s.sh dev
   scripts/deploy-k8s.sh staging
   scripts/deploy-k8s.sh production
   ```

The script applies the namespace, materializes the runtime Secret only when an ignored secret env file is present, renders Kustomize placeholders, applies the release, waits for the migration Job, then waits for all seven backend Deployments and the web Deployment. If the Secret already exists, set `KUBE_SECRET_ENV_FILE` to a secure temporary file or let the secret manager create it before running the script.

To inspect a release:

```sh
kubectl get deployments,hpa,pdb,networkpolicy -n opsmind-production
kubectl get pods -n opsmind-production
kubectl logs -n opsmind-production job/opsmind-migrations-"$OPSMIND_RELEASE_ID"
```

## Terraform boundary

The repository does not select a cloud provider. Terraform in `infra/terraform/` applies namespace-scoped `ResourceQuota` and `LimitRange` guardrails to a namespace created by Kustomize. Set the kubeconfig context and tune an environment tfvars file for cluster capacity:

```sh
cp infra/terraform/environments/production.tfvars.example infra/terraform/production.tfvars
cd infra/terraform
terraform init
terraform plan -var-file=production.tfvars
terraform apply -var-file=production.tfvars
```

Configure remote encrypted Terraform state and provider credentials before shared-environment use. Cloud-specific cluster, VPC, managed database, cache, secret-manager, and load-balancer resources belong in the organization's cloud modules; keeping that provider choice external avoids baking one cloud and its network ranges into this repository.

## Production gates and limitations

- API and remediation authentication default to deny-all. Supply the production identity-provider and authorization adapters before exposing APIs or enabling human approvals. The chart does not create an anonymous-access path.
- Configure an authenticated TLS ingress/gateway for the API and web UI; ingress and public hostnames are intentionally platform-specific. Route the web UI to `opsmind-web:8080`.
- Kubernetes Service traffic between Pods is plaintext in this baseline. Use a service mesh with mTLS or equivalent node/network encryption where the production threat model requires it.
- Replace the dev single-instance databases, Redis, Prometheus, Loki, and Jaeger with HA/managed services in staging and production. The dev telemetry stack is not a production availability design.
- The standard Kubernetes NetworkPolicy API does not provide FQDN rules. The external integration rule allows TCP 443 from incident, notification, and web Pods to any IPv4 destination; use a CNI with FQDN policies or an egress proxy to constrain LLM, Slack, and Prometheus proxy destinations further. Verify the configured Kubernetes API endpoint is reachable at the supplied CIDR and port from remediation Pods.
- The existing `infra/kubernetes/shopstack/` bundle is a development demo. It is not included in the OpsMind production overlays.
