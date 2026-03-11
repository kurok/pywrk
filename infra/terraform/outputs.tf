output "ecs_cluster_name" {
  description = "Name of the ECS cluster"
  value       = module.ecs_cluster.cluster_name
}

output "ecs_cluster_arn" {
  description = "ARN of the ECS cluster"
  value       = module.ecs_cluster.cluster_arn
}

output "master_service_name" {
  description = "ECS service name for the pywrkr master"
  value       = module.ecs_service_pywrkr.master_service_name
}

output "worker_service_name" {
  description = "ECS service name for the pywrkr workers"
  value       = module.ecs_service_pywrkr.worker_service_name
}

output "cloudmap_namespace_id" {
  description = "Cloud Map private DNS namespace ID"
  value       = module.ecs_service_pywrkr.cloudmap_namespace_id
}

output "cloudmap_namespace_name" {
  description = "Cloud Map private DNS namespace name"
  value       = var.cloudmap_namespace
}

output "master_log_group" {
  description = "CloudWatch log group for master"
  value       = module.cloudwatch.master_log_group_name
}

output "worker_log_group" {
  description = "CloudWatch log group for workers"
  value       = module.cloudwatch.worker_log_group_name
}

output "ecr_repository_url" {
  description = "ECR repository URL for pywrkr images"
  value       = module.ecr.repository_url
}

output "vpc_id" {
  description = "VPC ID"
  value       = module.network.vpc_id
}

output "reports_bucket" {
  description = "S3 bucket for HTML reports and JSON results"
  value       = module.ecs_service_pywrkr.reports_bucket
}
