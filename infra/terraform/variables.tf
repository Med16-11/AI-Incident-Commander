variable "kubeconfig_path" {
  description = "Path to the kubeconfig for the target cluster."
  type        = string
  default     = "~/.kube/config"
}

variable "kube_context" {
  description = "Optional kubeconfig context name."
  type        = string
  default     = null
  nullable    = true
}

variable "namespace" {
  description = "Namespace created by the selected Kustomize environment overlay."
  type        = string
}

variable "environment" {
  description = "Environment label used for guardrail resources."
  type        = string
  validation {
    condition     = contains(["dev", "staging", "production"], var.environment)
    error_message = "environment must be dev, staging, or production."
  }
}

variable "resource_quota" {
  description = "Kubernetes ResourceQuota hard limits, tuned for the target cluster."
  type        = map(string)
}

variable "container_default_limits" {
  description = "Default per-container limits applied when a workload omits a limit."
  type        = map(string)
  default     = { cpu = "1", memory = "1Gi" }
}

variable "container_default_requests" {
  description = "Default per-container requests applied when a workload omits requests."
  type        = map(string)
  default     = { cpu = "100m", memory = "128Mi" }
}

variable "container_maximums" {
  description = "Maximum per-container resource values."
  type        = map(string)
  default     = { cpu = "4", memory = "8Gi" }
}

variable "container_minimums" {
  description = "Minimum per-container resource values."
  type        = map(string)
  default     = { cpu = "10m", memory = "16Mi" }
}
