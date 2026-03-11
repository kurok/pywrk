variable "aws_region" {
  description = "AWS region for all resources"
  type        = string
  default     = "us-east-1"
}

variable "environment" {
  description = "Environment name (e.g. dev, staging, prod)"
  type        = string
  default     = "dev"
}

variable "project_name" {
  description = "Project name used for resource naming"
  type        = string
  default     = "pywrkr"
}

# --- VPC ---

variable "vpc_cidr" {
  description = "CIDR block for the VPC"
  type        = string
  default     = "10.0.0.0/16"
}

variable "enable_nat_gateway" {
  description = "Enable NAT gateway for private subnets (adds ~$32/mo per gateway). Set to false to use public subnets only for cost savings."
  type        = bool
  default     = false
}

# --- ECS ---

variable "master_cpu" {
  description = "CPU units for the master task (1024 = 1 vCPU)"
  type        = number
  default     = 1024
}

variable "master_memory" {
  description = "Memory in MiB for the master task"
  type        = number
  default     = 2048
}

variable "worker_cpu" {
  description = "CPU units for each worker task (1024 = 1 vCPU)"
  type        = number
  default     = 1024
}

variable "worker_memory" {
  description = "Memory in MiB for each worker task"
  type        = number
  default     = 2048
}

variable "worker_count" {
  description = "Number of pywrkr worker tasks"
  type        = number
  default     = 3
}

variable "container_image" {
  description = "Full container image URL (e.g. ghcr.io/kurok/pywrkr:latest)"
  type        = string
  default     = "ghcr.io/kurok/pywrkr:latest"
}

# --- pywrkr runtime ---

variable "target_url" {
  description = "Target URL for the load test"
  type        = string
  default     = "https://example.com"
}

variable "test_duration" {
  description = "Test duration in seconds"
  type        = number
  default     = 300
}

variable "connections" {
  description = "Number of concurrent connections per worker"
  type        = number
  default     = 100
}

variable "users" {
  description = "Number of virtual users (enables user simulation mode). Set to 0 to use connection mode."
  type        = number
  default     = 0
}

variable "rate" {
  description = "Target requests per second (0 = unlimited)"
  type        = number
  default     = 0
}

variable "thresholds" {
  description = "SLO threshold expressions (e.g. 'p95 < 300ms')"
  type        = list(string)
  default     = []
}

variable "scenario_file" {
  description = "Path to scenario file inside the container (e.g. /scenarios/api-test.json)"
  type        = string
  default     = ""
}

variable "tags" {
  description = "Metadata tags as key=value pairs for pywrkr"
  type        = map(string)
  default     = {}
}

variable "otel_endpoint" {
  description = "OpenTelemetry collector endpoint URL (optional)"
  type        = string
  default     = ""
}

variable "prom_remote_write" {
  description = "Prometheus Pushgateway endpoint URL (optional)"
  type        = string
  default     = ""
}

variable "cloudmap_namespace" {
  description = "Private DNS namespace for Cloud Map service discovery"
  type        = string
  default     = "pywrkr.local"
}

variable "log_retention_days" {
  description = "CloudWatch log retention in days"
  type        = number
  default     = 14
}
