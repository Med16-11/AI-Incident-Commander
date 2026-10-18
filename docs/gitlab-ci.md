# GitLab CI/CD pipeline

The root `.gitlab-ci.yml` runs the OpsMind release pipeline in eight ordered stages:

| Stage | Jobs and gates |
|---|---|
| `lint` | Ruff check/format and strict mypy for Python; TypeScript check and optimized Vite build for the web app. |
| `test` | Python unit suite, native Node frontend unit tests, and Go event-ingestion tests with race detector and vet. JUnit results are uploaded for Python. |
| `security` | Trivy dependency/secret scan, Terraform format/init/validate, and Kustomize render plus strict Kubernetes schema validation for all overlays. |
| `build` | Build and push the Python service, web, and Go event-ingestion images using rootless BuildKit. |
| `scan` | Trivy scans all three pushed images; HIGH and CRITICAL findings fail the job. |
| `integration-test` | API integration tests, Alembic migration against disposable PostgreSQL, and a real PostgreSQL schema/Redis Streams smoke check. |
| `package` | CycloneDX SBOM artifacts for all images and keyless Cosign signatures for default-branch release images. |
| `deploy` | Automatically deploys default-branch releases to staging. Production is a blocking manual job. |

GitLab stage ordering blocks every deployment if a preceding lint, test, dependency, infrastructure validation, image scan, integration, SBOM, or signing job fails. Production is only present on the default branch and uses `allow_failure: false`; protect the GitLab `production` environment and restrict its allowed deployers/approvers to make the manual action a meaningful approval gate. Staging deploys automatically from the default branch once every preceding stage succeeds. Merge requests build and scan images but do not deploy or sign them.

## Image identity and supply chain

Builds publish unique traceability tags of the form `<full-commit-sha>-<pipeline-id>`. The build job exports the registry digest as a dotenv artifact, and all later scanning, signing, and Kubernetes deployment use the digest reference (`repository@sha256:...`), never a floating tag. The pipeline does not publish `latest`.

Trivy checks lockfiles and source for HIGH/CRITICAL dependency vulnerabilities and secrets, then scans both built images at the same severity threshold. SBOMs are emitted as CycloneDX JSON artifacts and retained for one year. On the default branch, Cosign uses a GitLab OIDC ID token for keyless signing. Configure GitLab OIDC access, Sigstore Fulcio/Rekor reachability, and registry write permissions on the protected release runner. Consumers should verify signatures against the expected GitLab project and protected ref identity before pulling images.

## Runner and project configuration

Use protected Linux/amd64 runners with network access to the GitLab container registry, PyPI/npm/Go module registries, GitHub release downloads for pinned validation tools, the Kubernetes schema registry, and Sigstore endpoints. Rootless BuildKit is used with its process sandbox enabled; configure the runner kernel/seccomp and mount namespace support required by rootless BuildKit. Do not enable privileged Docker-in-Docker or disable BuildKit's process sandbox for this pipeline.

Configure the following GitLab CI/CD variables. Mark credentials as **masked and protected**, use **file** type for kubeconfigs, and scope deployment values to their matching environment. Prefer syncing credentials from a secret manager. Never define populated credentials in `.gitlab-ci.yml`.

| Variable | Scope | Purpose |
|---|---|---|
| `KUBE_CONFIG` (file) | `staging`, `production` | Kubeconfig for the environment's deploy identity. Limit staging and production identities to their own OpsMind namespace, target namespace, Secret access needed for rollout, and namespaced workload permissions. |
| `OPSMIND_TARGET_NAMESPACE` | each environment | Existing dedicated namespace whose workloads the remediation Role can inspect/patch. |
| `OPSMIND_KUBERNETES_API_CIDR`, `OPSMIND_KUBERNETES_API_PORT` | each environment | Egress destination for the Kubernetes API used by the remediation adapter. |
| `OPSMIND_DB_CIDR`, `OPSMIND_REDIS_CIDR`, `OPSMIND_OBSERVABILITY_CIDR`, `OPSMIND_METRICS_CIDR` | staging, production | Narrow managed-service and scraper ranges consumed by the NetworkPolicies. |
| `KUBE_SECRET_ENV_FILE` (file, optional) | deployment environment | Optional env-file source used by the deploy script to create/update `opsmind-runtime`. Prefer an external secret controller instead. |

The `opsmind-runtime` Secret must already exist in the OpsMind namespace when no secret env file is supplied. Store database, Redis, observability, LLM, Slack, and internal service credentials there through the environment's secret manager. Staging and production are intentionally not given example passwords.

Create the `staging` and `production` namespaces, target namespaces, quotas/LimitRanges, managed services, ingress, NetworkPolicy-capable CNI, Metrics Server, and secret-manager integration before enabling deploy jobs. Ensure cluster nodes or a namespaced `imagePullSecret` can pull from the GitLab Container Registry; registry pull credentials are not embedded in workload manifests. Terraform validation runs in CI; Terraform apply remains an operator-controlled infrastructure change and is not run by application deployment jobs.

## Rollout and rollback

The staging job waits for the release migration Job, all seven backend Deployments, and the web Deployment to become available. After staging verification, an authorized user starts the blocking `deploy-production` manual job. The job deploys the same signed image digests tested in staging. To roll back, redeploy a previously approved pipeline's digest artifacts through the protected production environment; do not retag a mutable image. Database migrations must remain backward compatible across the rollback window.

## Current test boundary

Frontend tests use Node's built-in test runner and cover structured API error parsing. Python integration tests currently use test fixtures; the dedicated CI smoke step adds live PostgreSQL migration/schema and Redis Streams connectivity checks. Cluster-specific Kubernetes adapter tests still require a local Kubernetes cluster and are not silently claimed as CI coverage.
