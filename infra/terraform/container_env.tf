resource "azurerm_container_app_environment" "this" {
  name                       = local.aca_env_name
  resource_group_name        = azurerm_resource_group.this.name
  location                   = azurerm_resource_group.this.location
  log_analytics_workspace_id = azurerm_log_analytics_workspace.this.id

  # Azure adds this built-in serverless profile to every new environment. Declaring it keeps
  # the configuration in step with reality (otherwise every plan wants to remove it).
  workload_profile {
    name                  = "Consumption"
    workload_profile_type = "Consumption"
  }

  tags = var.tags
}
