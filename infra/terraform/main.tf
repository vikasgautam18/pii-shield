terraform {
  required_version = ">= 1.5"

  # Configure the Azure Storage backend at `terraform init` time:
  #   terraform init \
  #     -backend-config="resource_group_name=<rg>" \
  #     -backend-config="storage_account_name=<sa>" \
  #     -backend-config="container_name=tfstate" \
  #     -backend-config="key=pii-shield.terraform.tfstate" \
  #     -backend-config="use_azuread_auth=true"
  #
  # See infra/scripts/00-bootstrap-state.sh to provision the state container.
  backend "azurerm" {}

  required_providers {
    azurerm = {
      source  = "hashicorp/azurerm"
      version = "~> 4.0"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.6"
    }
  }
}

provider "azurerm" {
  features {
    resource_group {
      prevent_deletion_if_contains_resources = false
    }
  }
  subscription_id = var.subscription_id
}

# Random suffix for globally-unique resource names (ACR, Redis, Grafana).
# Persisted in Terraform state; regenerated only if the resource is tainted
# or name_suffix is changed.
resource "random_string" "suffix" {
  length  = 5
  upper   = false
  special = false
  numeric = true
}

locals {
  # Resolved suffix: user-supplied wins, else the auto-generated random string.
  suffix = var.name_suffix != "" ? var.name_suffix : random_string.suffix.result

  # Resource names
  # - Globally-unique resources (DNS namespace) use prefix + suffix.
  # - RG-scoped resources use prefix only (predictable names within your RG).
  resource_group_name = "${var.project_name}-rg"
  acr_name            = substr("${var.project_name}acr${local.suffix}", 0, 50) # alphanumeric only, max 50
  redis_name          = "${var.project_name}-redis-${local.suffix}"
  grafana_name        = substr("${var.project_name}-g-${local.suffix}", 0, 23) # Azure Grafana max 23 chars
  aca_env_name        = "${var.project_name}-env"
  log_analytics_name  = "${var.project_name}-logs"
  app_insights_name   = "${var.project_name}-insights"

  # Default container app names (consumed by infra/scripts/02-deploy-apps.sh)
  api_app_name        = var.project_name
  playground_app_name = "${var.project_name}-playground"
  admin_app_name      = "${var.project_name}-admin"
}

resource "azurerm_resource_group" "this" {
  name     = local.resource_group_name
  location = var.location

  tags = var.tags
}
