output "master_log_group_name" {
  value = aws_cloudwatch_log_group.master.name
}

output "master_log_group_arn" {
  value = aws_cloudwatch_log_group.master.arn
}

output "worker_log_group_name" {
  value = aws_cloudwatch_log_group.worker.name
}

output "worker_log_group_arn" {
  value = aws_cloudwatch_log_group.worker.arn
}
