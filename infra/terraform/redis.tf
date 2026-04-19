resource "azurerm_redis_cache" "this" {
  name                          = local.redis_name
  resource_group_name           = azurerm_resource_group.this.name
  location                      = azurerm_resource_group.this.location
  capacity                      = var.redis_capacity
  family                        = var.redis_family
  sku_name                      = var.redis_sku
  non_ssl_port_enabled          = false
  minimum_tls_version           = "1.2"
  public_network_access_enabled = true

  # Entra (AAD) authentication — disable access keys for compliance.
  # Set to true temporarily during migration; false once all apps use Entra.
  access_keys_authentication_enabled = var.redis_access_keys_enabled

  redis_configuration {
    # Entra ID auth must be enabled before access keys can be disabled.
    active_directory_authentication_enabled = !var.redis_access_keys_enabled
  }

  tags = var.tags
}
