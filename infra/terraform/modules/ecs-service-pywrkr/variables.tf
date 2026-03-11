variable "name_prefix" {
  type = string
}

variable "aws_region" {
  type = string
}

variable "cluster_id" {
  type = string
}

variable "vpc_id" {
  type = string
}

variable "subnet_ids" {
  type = list(string)
}

variable "assign_public_ip" {
  type    = bool
  default = true
}

variable "cloudmap_namespace" {
  type = string
}

variable "execution_role_arn" {
  type = string
}

variable "task_role_arn" {
  type = string
}

variable "ecr_repository_url" {
  type = string
}

variable "image_tag" {
  type    = string
  default = "latest"
}

variable "master_cpu" {
  type    = number
  default = 1024
}

variable "master_memory" {
  type    = number
  default = 2048
}

variable "worker_cpu" {
  type    = number
  default = 1024
}

variable "worker_memory" {
  type    = number
  default = 2048
}

variable "worker_count" {
  type    = number
  default = 3
}

variable "master_log_group" {
  type = string
}

variable "worker_log_group" {
  type = string
}

variable "target_url" {
  type = string
}

variable "test_duration" {
  type    = number
  default = 300
}

variable "connections" {
  type    = number
  default = 100
}

variable "users" {
  type    = number
  default = 0
}

variable "rate" {
  type    = number
  default = 0
}

variable "thresholds" {
  type    = list(string)
  default = []
}

variable "scenario_file" {
  type    = string
  default = ""
}

variable "tags" {
  type    = map(string)
  default = {}
}

variable "otel_endpoint" {
  type    = string
  default = ""
}

variable "prom_remote_write" {
  type    = string
  default = ""
}
