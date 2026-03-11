variable "name_prefix" {
  description = "Prefix for resource names"
  type        = string
}

variable "retention_days" {
  description = "Log retention in days"
  type        = number
  default     = 14
}
