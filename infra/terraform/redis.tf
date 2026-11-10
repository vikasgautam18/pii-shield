# ── Azure Managed Redis ──────────────────────────────────────────────────────
# Replaces classic Azure Cache for Redis (Basic/Standard/Premium): creating new
# classic caches has been blocked for new customers since 1 Apr 2026, and the
# product retires on 30 Sep 2028.
#
# - Microsoft Entra authentication only (access keys are off by default). The API
#   signs in with its managed identity, which infra/scripts/02-deploy-apps.sh grants
#   access to after the app exists.
# - The EnterpriseCluster policy exposes a single endpoint, so plain (non cluster
#   aware) clients such as the app's redis-py pool work. The default OSSCluster
#   policy would need a cluster-aware client.

resource "azurerm_managed_redis" "this" {
  name                      = local.redis_name
  resource_group_name       = azurerm_resource_group.this.name
  location                  = azurerm_resource_group.this.location
  sku_name                  = var.redis_sku_name
  high_availability_enabled = var.redis_high_availability_enabled
  public_network_access     = "Enabled"

  default_database {
    # Entra authentication is always available. Access keys are an opt-in fallback.
    access_keys_authentication_enabled = var.redis_access_keys_enabled
    client_protocol                    = "Encrypted"
    clustering_policy                  = "EnterpriseCluster"
    eviction_policy                    = "VolatileLRU"
  }

  tags = var.tags
}

# Lets the operator who runs Terraform sign in with Entra, for example
# `az redisenterprise test-connection`. The app's identity is granted access by
# infra/scripts/02-deploy-apps.sh once the container app (and its identity) exist.
resource "azurerm_managed_redis_access_policy_assignment" "operator" {
  managed_redis_id = azurerm_managed_redis.this.id
  object_id        = local.operator_object_id
}
