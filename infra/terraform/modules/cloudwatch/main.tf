resource "aws_cloudwatch_log_group" "master" {
  name              = "/ecs/${var.name_prefix}/master"
  retention_in_days = var.retention_days
}

resource "aws_cloudwatch_log_group" "worker" {
  name              = "/ecs/${var.name_prefix}/worker"
  retention_in_days = var.retention_days
}
