# ── ACR ──────────────────────────────────────────────────────────────────────

output "acr_login_server" {
  description = "ACR login server URL"
  value       = azurerm_container_registry.this.login_server
}

output "acr_admin_username" {
  description = "ACR admin username"
  value       = azurerm_container_registry.this.admin_username
}

output "acr_admin_password" {
  description = "ACR admin password"
  value       = azurerm_container_registry.this.admin_password
  sensitive   = true
}

# ── Redis ────────────────────────────────────────────────────────────────────

output "redis_hostname" {
  description = "Redis hostname"
  value       = azurerm_redis_cache.this.hostname
}

output "redis_ssl_port" {
  description = "Redis TLS port"
  value       = azurerm_redis_cache.this.ssl_port
}

output "redis_primary_key" {
  description = "Redis primary access key"
  value       = azurerm_redis_cache.this.primary_access_key
  sensitive   = true
}

output "redis_url" {
  description = "Full Redis connection URL (rediss:// with TLS)"
  value       = "rediss://:${urlencode(azurerm_redis_cache.this.primary_access_key)}@${azurerm_redis_cache.this.hostname}:${azurerm_redis_cache.this.ssl_port}/0"
  sensitive   = true
}

# ── Container Apps Environment ───────────────────────────────────────────────

output "aca_environment_id" {
  description = "Container Apps Environment resource ID"
  value       = azurerm_container_app_environment.this.id
}

output "aca_environment_name" {
  description = "Container Apps Environment name"
  value       = azurerm_container_app_environment.this.name
}

# ── Application Insights ────────────────────────────────────────────────────

output "app_insights_connection_string" {
  description = "Application Insights connection string"
  value       = azurerm_application_insights.this.connection_string
  sensitive   = true
}

output "app_insights_name" {
  description = "Application Insights resource name"
  value       = azurerm_application_insights.this.name
}

output "app_insights_instrumentation_key" {
  description = "Application Insights instrumentation key"
  value       = azurerm_application_insights.this.instrumentation_key
  sensitive   = true
}

# ── Resource Group ───────────────────────────────────────────────────────────

output "resource_group_name" {
  description = "Resource group name"
  value       = azurerm_resource_group.this.name
}

output "location" {
  description = "Azure region"
  value       = azurerm_resource_group.this.location
}

# ── Naming ───────────────────────────────────────────────────────────────────

output "project_name" {
  description = "Project name prefix used for all resources"
  value       = var.project_name
}

output "name_suffix" {
  description = "Suffix used for globally-unique resource names"
  value       = local.suffix
}

output "api_app_name" {
  description = "Default name for the FastAPI container app"
  value       = local.api_app_name
}

output "playground_app_name" {
  description = "Default name for the Streamlit playground container app"
  value       = local.playground_app_name
}

output "admin_app_name" {
  description = "Default name for the Streamlit admin container app"
  value       = local.admin_app_name
}

# ── Managed Grafana ─────────────────────────────────────────────────────────

output "grafana_url" {
  description = "Azure Managed Grafana dashboard URL"
  value       = azurerm_dashboard_grafana.this.endpoint
}

output "grafana_name" {
  description = "Azure Managed Grafana instance name"
  value       = azurerm_dashboard_grafana.this.name
}
