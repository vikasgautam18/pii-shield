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

variable "operator_object_id" {
  description = "Object ID of the user or service principal that runs Terraform. It receives Grafana Admin and Managed Redis access. Leave empty to use the signed-in identity (recommended)."
  type        = string
  default     = ""
}

# ── Azure Container Registry ────────────────────────────────────────────────

variable "acr_sku" {
  description = "ACR SKU tier"
  type        = string
  default     = "Basic"
}

# ── Azure Managed Redis ─────────────────────────────────────────────────────

variable "redis_sku_name" {
  description = "Azure Managed Redis SKU, for example Balanced_B0 (smallest, dev/test), Balanced_B1 or Balanced_B3."
  type        = string
  default     = "Balanced_B0"

  validation {
    condition     = can(regex("^(Balanced_B|ComputeOptimized_X|MemoryOptimized_M|FlashOptimized_A)[0-9]+$", var.redis_sku_name))
    error_message = "redis_sku_name must look like Balanced_B0, Balanced_B3, ComputeOptimized_X10, MemoryOptimized_M10 or FlashOptimized_A250."
  }
}

variable "redis_high_availability_enabled" {
  description = "Run Managed Redis with high availability. Off by default (cheaper, fine for dev/test): data can be lost on a node failure or maintenance. Changing this forces a new cache."
  type        = bool
  default     = false
}

variable "redis_access_keys_enabled" {
  description = "Enable access key authentication. Default is false (Entra ID-only auth): the app signs in with its managed identity, which 02-deploy-apps.sh grants access to."
  type        = bool
  default     = false
}

# ── Azure Managed Grafana ───────────────────────────────────────────────────

variable "grafana_major_version" {
  description = "Azure Managed Grafana major version. Azure only accepts the versions it currently supports for new instances (12 and 13 when this was last checked; 11 is rejected for the Standard SKU)."
  type        = string
  default     = "12"
}
