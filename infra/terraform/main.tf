terraform {
  required_version = ">= 1.6.0, < 2.0.0"
  required_providers {
    kubernetes = {
      source  = "hashicorp/kubernetes"
      version = ">= 2.30.0, < 3.0.0"
    }
  }
}

provider "kubernetes" {
  config_path    = var.kubeconfig_path
  config_context = var.kube_context
}

resource "kubernetes_resource_quota_v1" "opsmind" {
  metadata {
    name      = "opsmind-quota"
    namespace = var.namespace
    labels = {
      "opsmind.io/environment" = var.environment
      "app.kubernetes.io/part-of" = "opsmind"
    }
  }

  spec {
    hard = var.resource_quota
  }
}

resource "kubernetes_limit_range_v1" "opsmind" {
  metadata {
    name      = "opsmind-container-defaults"
    namespace = var.namespace
    labels = {
      "opsmind.io/environment" = var.environment
      "app.kubernetes.io/part-of" = "opsmind"
    }
  }

  spec {
    limit {
      type           = "Container"
      default        = var.container_default_limits
      default_request = var.container_default_requests
      max            = var.container_maximums
      min            = var.container_minimums
    }
  }
}
