output "namespace" {
  description = "Namespace receiving the OpsMind guardrails."
  value       = var.namespace
}

output "resource_quota_name" {
  value = kubernetes_resource_quota_v1.opsmind.metadata[0].name
}

output "limit_range_name" {
  value = kubernetes_limit_range_v1.opsmind.metadata[0].name
}
