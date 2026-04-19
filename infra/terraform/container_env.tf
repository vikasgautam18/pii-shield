resource "azurerm_container_app_environment" "this" {
  name                       = local.aca_env_name
  resource_group_name        = azurerm_resource_group.this.name
  location                   = azurerm_resource_group.this.location
  log_analytics_workspace_id = azurerm_log_analytics_workspace.this.id

  tags = var.tags
}
