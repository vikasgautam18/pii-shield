# ── Azure Managed Grafana ────────────────────────────────────────────────────

resource "azurerm_dashboard_grafana" "this" {
  name                              = local.grafana_name
  resource_group_name               = azurerm_resource_group.this.name
  location                          = azurerm_resource_group.this.location
  sku                               = "Standard"
  grafana_major_version             = 11
  api_key_enabled                   = true
  deterministic_outbound_ip_enabled = false
  public_network_access_enabled     = true

  identity {
    type = "SystemAssigned"
  }

  tags = var.tags
}

# ── RBAC: Grant Grafana managed identity read access ────────────────────────

# Monitoring Reader on the resource group (metrics)
resource "azurerm_role_assignment" "grafana_monitoring_reader" {
  principal_id         = azurerm_dashboard_grafana.this.identity[0].principal_id
  role_definition_name = "Monitoring Reader"
  scope                = azurerm_resource_group.this.id
}

# Log Analytics Reader (logs)
resource "azurerm_role_assignment" "grafana_log_reader" {
  principal_id         = azurerm_dashboard_grafana.this.identity[0].principal_id
  role_definition_name = "Log Analytics Reader"
  scope                = azurerm_log_analytics_workspace.this.id
}

# Grant the current user Grafana Admin so they can manage dashboards
data "azurerm_client_config" "current" {}

resource "azurerm_role_assignment" "grafana_admin" {
  principal_id         = data.azurerm_client_config.current.object_id
  role_definition_name = "Grafana Admin"
  scope                = azurerm_dashboard_grafana.this.id
}
