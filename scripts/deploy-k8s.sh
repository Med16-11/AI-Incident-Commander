#!/usr/bin/env bash
set -euo pipefail

environment="${1:-}"
case "$environment" in
  dev|staging|production) ;;
  *) echo "usage: $0 {dev|staging|production}" >&2; exit 2 ;;
esac

: "${OPSMIND_IMAGE:?Set OPSMIND_IMAGE to the immutable image reference for this release}"
: "${OPSMIND_WEB_IMAGE:?Set OPSMIND_WEB_IMAGE to the immutable web image reference for this release}"
: "${OPSMIND_TARGET_NAMESPACE:?Set OPSMIND_TARGET_NAMESPACE to the namespace containing monitored workloads}"
: "${OPSMIND_KUBERNETES_API_CIDR:?Set OPSMIND_KUBERNETES_API_CIDR to the cluster API endpoint CIDR}"
: "${OPSMIND_KUBERNETES_API_PORT:?Set OPSMIND_KUBERNETES_API_PORT to the cluster API endpoint port}"
: "${OPSMIND_RELEASE_ID:?Set OPSMIND_RELEASE_ID to a unique lowercase release identifier}"
if [[ ! "$OPSMIND_RELEASE_ID" =~ ^[a-z0-9]([a-z0-9-]{0,38}[a-z0-9])?$ ]]; then
  echo "OPSMIND_RELEASE_ID must be a DNS-safe lowercase name of at most 40 characters" >&2
  exit 2
fi

case "$environment" in
  dev) namespace="opsmind-dev" ;;
  staging) namespace="opsmind-staging" ;;
  production) namespace="opsmind-production" ;;
esac
overlay="infra/kubernetes/opsmind/overlays/$environment"
secret_file="${KUBE_SECRET_ENV_FILE:-$overlay/secrets.env}"

if [[ "$environment" != "dev" ]]; then
  : "${OPSMIND_DB_CIDR:?Set OPSMIND_DB_CIDR to the managed database endpoint CIDR}"
  : "${OPSMIND_REDIS_CIDR:?Set OPSMIND_REDIS_CIDR to the managed Redis endpoint CIDR}"
  : "${OPSMIND_OBSERVABILITY_CIDR:?Set OPSMIND_OBSERVABILITY_CIDR to the metrics/logs/traces endpoint CIDR}"
  : "${OPSMIND_METRICS_CIDR:?Set OPSMIND_METRICS_CIDR to the metrics scraper source CIDR}"
fi

kubectl apply -f "$overlay/namespace.yaml"
if ! kubectl get namespace "$OPSMIND_TARGET_NAMESPACE" >/dev/null 2>&1; then
  echo "Target namespace $OPSMIND_TARGET_NAMESPACE must exist before remediation RBAC is applied" >&2
  exit 2
fi
if [[ -f "$secret_file" ]]; then
  if grep -Eq 'CHANGE_ME|REQUIRED_FROM_' "$secret_file"; then
    echo "Refusing to deploy placeholder values from $secret_file" >&2
    exit 2
  fi
  kubectl create secret generic opsmind-runtime \
    --namespace "$namespace" \
    --from-env-file="$secret_file" \
    --dry-run=client -o yaml | kubectl apply -f -
elif ! kubectl get secret opsmind-runtime --namespace "$namespace" >/dev/null 2>&1; then
  echo "Secret opsmind-runtime is missing in $namespace; provision it from the environment secret manager" >&2
  exit 2
fi

export OPSMIND_NAMESPACE="$namespace"
kubectl kustomize "$overlay" \
  | envsubst '${OPSMIND_IMAGE} ${OPSMIND_WEB_IMAGE} ${OPSMIND_RELEASE_ID} ${OPSMIND_NAMESPACE} ${OPSMIND_TARGET_NAMESPACE} ${OPSMIND_KUBERNETES_API_CIDR} ${OPSMIND_KUBERNETES_API_PORT} ${OPSMIND_DB_CIDR} ${OPSMIND_REDIS_CIDR} ${OPSMIND_OBSERVABILITY_CIDR} ${OPSMIND_METRICS_CIDR}' \
  | kubectl apply -f -

envsubst '${OPSMIND_NAMESPACE} ${OPSMIND_TARGET_NAMESPACE}' \
  < infra/kubernetes/opsmind/target-rbac.yaml \
  | kubectl apply -f -

kubectl wait --for=condition=complete \
  --timeout=10m \
  "job/opsmind-migrations-$OPSMIND_RELEASE_ID" \
  --namespace "$namespace"

for deployment in \
  opsmind-api \
  opsmind-incident-engine \
  opsmind-detection-engine \
  opsmind-remediation-engine \
  opsmind-notification-service \
  opsmind-worker \
  opsmind-web; do
  kubectl rollout status "deployment/$deployment" --namespace "$namespace" --timeout=5m
done

kubectl get deployments,hpa,pdb,networkpolicy --namespace "$namespace"
