output "master_service_name" {
  value = aws_ecs_service.master.name
}

output "master_service_arn" {
  value = aws_ecs_service.master.id
}

output "worker_service_name" {
  value = aws_ecs_service.worker.name
}

output "worker_service_arn" {
  value = aws_ecs_service.worker.id
}

output "cloudmap_namespace_id" {
  value = aws_service_discovery_private_dns_namespace.main.id
}

output "master_task_definition_arn" {
  value = aws_ecs_task_definition.master.arn
}

output "worker_task_definition_arn" {
  value = aws_ecs_task_definition.worker.arn
}

output "reports_bucket" {
  value = aws_s3_bucket.reports.bucket
}
