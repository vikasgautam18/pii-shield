# ── Core naming ──────────────────────────────────────────────────────────────
# All resource names are derived from `project_name` + an optional suffix.
# The suffix is required for globally-unique resources (ACR, Redis, Grafana).
# If `name_suffix` is left empty, a random 5-character suffix is generated
# and persisted in the Terraform state.

variable "project_name" {
  description = "Short project name used as a prefix for all resources (3-16 lowercase alphanumeric chars)"
  type        = string
  default     = "piishield"

  validation {
    condition     = can(regex("^[a-z0-9]{3,16}$", var.project_name))
    error_message = "project_name must be 3-16 characters, lowercase letters and digits only."
  }
}

variable "name_suffix" {
  description = "Optional suffix for globally-unique resource names. Leave empty to auto-generate a random 5-char suffix."
  type        = string
  default     = ""

  validation {
    condition     = var.name_suffix == "" || can(regex("^[a-z0-9]{2,8}$", var.name_suffix))
    error_message = "name_suffix must be empty or 2-8 lowercase alphanumeric chars."
  }
}

# ── General ──────────────────────────────────────────────────────────────────

variable "subscription_id" {
  description = "Azure subscription ID"
  type        = string
}

variable "location" {
  description = "Azure region for all resources"
  type        = string
  default     = "centralindia"
}

variable "tags" {
  description = "Tags applied to all resources"
  type        = map(string)
  default = {
    project    = "pii-shield"
    managed_by = "terraform"
  }
}

# ── Azure Container Registry ────────────────────────────────────────────────

variable "acr_sku" {
  description = "ACR SKU tier"
  type        = string
  default     = "Basic"
}

# ── Azure Cache for Redis ───────────────────────────────────────────────────

variable "redis_sku" {
  description = "Redis SKU (Basic, Standard, Premium)"
  type        = string
  default     = "Standard"
}

variable "redis_capacity" {
  description = "Redis cache capacity (0 = C0, 1 = C1, etc.)"
  type        = number
  default     = 1
}

variable "redis_family" {
  description = "Redis family (C = Basic/Standard, P = Premium)"
  type        = string
  default     = "C"
}

variable "redis_access_keys_enabled" {
  description = "Enable access key authentication. Default is false (Entra ID-only auth). The app uses managed identity + Redis 'Data Owner' access policy when this is false."
  type        = bool
  default     = false
}
