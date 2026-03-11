###############################################################################
# Cloud Map — private DNS so workers can discover master
###############################################################################

resource "aws_service_discovery_private_dns_namespace" "main" {
  name = var.cloudmap_namespace
  vpc  = var.vpc_id
}

resource "aws_service_discovery_service" "master" {
  name = "pywrkr-master"

  dns_config {
    namespace_id = aws_service_discovery_private_dns_namespace.main.id

    dns_records {
      ttl  = 10
      type = "A"
    }

    routing_policy = "MULTIVALUE"
  }

  health_check_custom_config {
    failure_threshold = 1
  }
}

###############################################################################
# Security Groups
###############################################################################

resource "aws_security_group" "master" {
  name_prefix = "${var.name_prefix}-master-"
  vpc_id      = var.vpc_id

  # Workers connect to master on TCP 9000
  ingress {
    from_port       = 9000
    to_port         = 9000
    protocol        = "tcp"
    security_groups = [aws_security_group.worker.id]
    description     = "pywrkr worker to master coordination"
  }

  # Outbound: HTTPS to target + DNS + ECR/CloudWatch
  egress {
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
    description = "HTTPS outbound (target URL, ECR, CloudWatch)"
  }

  egress {
    from_port   = 80
    to_port     = 80
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
    description = "HTTP outbound (target URL)"
  }

  egress {
    from_port   = 53
    to_port     = 53
    protocol    = "udp"
    cidr_blocks = ["0.0.0.0/0"]
    description = "DNS"
  }

  egress {
    from_port   = 53
    to_port     = 53
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
    description = "DNS over TCP"
  }

  tags = { Name = "${var.name_prefix}-master-sg" }

  lifecycle {
    create_before_destroy = true
  }
}

resource "aws_security_group" "worker" {
  name_prefix = "${var.name_prefix}-worker-"
  vpc_id      = var.vpc_id

  # Outbound: reach master on 9000
  egress {
    from_port   = 9000
    to_port     = 9000
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
    description = "Worker to master coordination"
  }

  # Outbound: HTTPS to target + ECR/CloudWatch
  egress {
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
    description = "HTTPS outbound (target URL, ECR, CloudWatch)"
  }

  egress {
    from_port   = 80
    to_port     = 80
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
    description = "HTTP outbound (target URL)"
  }

  egress {
    from_port   = 53
    to_port     = 53
    protocol    = "udp"
    cidr_blocks = ["0.0.0.0/0"]
    description = "DNS"
  }

  egress {
    from_port   = 53
    to_port     = 53
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
    description = "DNS over TCP"
  }

  tags = { Name = "${var.name_prefix}-worker-sg" }

  lifecycle {
    create_before_destroy = true
  }
}

###############################################################################
# Master Task Definition
###############################################################################

locals {
  # Build the pywrkr master command dynamically
  master_base_cmd = compact([
    "--master",
    "--expect-workers", tostring(var.worker_count),
    "--bind", "0.0.0.0",
    "--port", "9000",
  ])

  # URL or scenario mode
  master_url_cmd = var.scenario_file != "" ? [
    "--scenario", var.scenario_file,
  ] : []

  master_duration_cmd = ["-d", tostring(var.test_duration)]

  master_connections_cmd = var.users == 0 ? [
    "-c", tostring(var.connections),
  ] : []

  master_users_cmd = var.users > 0 ? [
    "-u", tostring(var.users),
  ] : []

  master_rate_cmd = var.rate > 0 ? [
    "--rate", tostring(var.rate),
  ] : []

  master_threshold_cmd = flatten([
    for th in var.thresholds : ["--threshold", th]
  ])

  master_tag_cmd = flatten([
    for k, v in var.tags : ["--tag", "${k}=${v}"]
  ])

  master_otel_cmd = var.otel_endpoint != "" ? [
    "--otel-endpoint", var.otel_endpoint,
  ] : []

  master_prom_cmd = var.prom_remote_write != "" ? [
    "--prom-remote-write", var.prom_remote_write,
  ] : []

  # Final command: base + mode + options + URL (URL must be last positional arg)
  master_command = concat(
    local.master_base_cmd,
    local.master_duration_cmd,
    local.master_connections_cmd,
    local.master_users_cmd,
    local.master_rate_cmd,
    local.master_threshold_cmd,
    local.master_tag_cmd,
    local.master_otel_cmd,
    local.master_prom_cmd,
    local.master_url_cmd,
    var.scenario_file == "" ? [var.target_url] : [],
  )

  # Worker command: connect to master via Cloud Map DNS
  worker_command = [
    "--worker",
    "pywrkr-master.${var.cloudmap_namespace}:9000",
  ]

  image = "${var.ecr_repository_url}:${var.image_tag}"
}

resource "aws_ecs_task_definition" "master" {
  family                   = "${var.name_prefix}-master"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = var.master_cpu
  memory                   = var.master_memory
  execution_role_arn       = var.execution_role_arn
  task_role_arn            = var.task_role_arn

  container_definitions = jsonencode([{
    name      = "pywrkr-master"
    image     = local.image
    essential = true
    command   = local.master_command

    portMappings = [{
      containerPort = 9000
      protocol      = "tcp"
    }]

    environment = concat(
      [{ name = "PYWRKR_ROLE", value = "master" }],
      var.otel_endpoint != "" ? [{ name = "OTEL_EXPORTER_OTLP_ENDPOINT", value = var.otel_endpoint }] : [],
      var.prom_remote_write != "" ? [{ name = "PYWRKR_PROM_ENDPOINT", value = var.prom_remote_write }] : [],
    )

    logConfiguration = {
      logDriver = "awslogs"
      options = {
        "awslogs-group"         = var.master_log_group
        "awslogs-region"        = var.aws_region
        "awslogs-stream-prefix" = "master"
      }
    }
  }])
}

###############################################################################
# Worker Task Definition
###############################################################################

resource "aws_ecs_task_definition" "worker" {
  family                   = "${var.name_prefix}-worker"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = var.worker_cpu
  memory                   = var.worker_memory
  execution_role_arn       = var.execution_role_arn
  task_role_arn            = var.task_role_arn

  container_definitions = jsonencode([{
    name      = "pywrkr-worker"
    image     = local.image
    essential = true
    command   = local.worker_command

    environment = [
      { name = "PYWRKR_ROLE", value = "worker" },
    ]

    logConfiguration = {
      logDriver = "awslogs"
      options = {
        "awslogs-group"         = var.worker_log_group
        "awslogs-region"        = var.aws_region
        "awslogs-stream-prefix" = "worker"
      }
    }
  }])
}

###############################################################################
# ECS Services
###############################################################################

resource "aws_ecs_service" "master" {
  name            = "${var.name_prefix}-master"
  cluster         = var.cluster_id
  task_definition = aws_ecs_task_definition.master.arn
  desired_count   = 1
  launch_type     = "FARGATE"

  enable_execute_command = true

  network_configuration {
    subnets          = var.subnet_ids
    security_groups  = [aws_security_group.master.id]
    assign_public_ip = var.assign_public_ip
  }

  service_registries {
    registry_arn = aws_service_discovery_service.master.arn
  }

  # Allow re-deployment to update command/image
  force_new_deployment = true

  deployment_minimum_healthy_percent = 0
  deployment_maximum_percent         = 200
}

resource "aws_ecs_service" "worker" {
  name            = "${var.name_prefix}-worker"
  cluster         = var.cluster_id
  task_definition = aws_ecs_task_definition.worker.arn
  desired_count   = var.worker_count
  launch_type     = "FARGATE"

  enable_execute_command = true

  network_configuration {
    subnets          = var.subnet_ids
    security_groups  = [aws_security_group.worker.id]
    assign_public_ip = var.assign_public_ip
  }

  # Workers must start after master is registered in Cloud Map
  depends_on = [aws_ecs_service.master]

  deployment_minimum_healthy_percent = 0
  deployment_maximum_percent         = 200
}

###############################################################################
# Auto Scaling (placeholder — defaults to fixed count)
###############################################################################

resource "aws_appautoscaling_target" "worker" {
  max_capacity       = var.worker_count * 3
  min_capacity       = var.worker_count
  resource_id        = "service/${split("/", var.cluster_id)[1]}/${aws_ecs_service.worker.name}"
  scalable_dimension = "ecs:service:DesiredCount"
  service_namespace  = "ecs"
}

# Uncomment to enable target-tracking auto scaling:
# resource "aws_appautoscaling_policy" "worker_cpu" {
#   name               = "${var.name_prefix}-worker-cpu"
#   policy_type        = "TargetTrackingScaling"
#   resource_id        = aws_appautoscaling_target.worker.resource_id
#   scalable_dimension = aws_appautoscaling_target.worker.scalable_dimension
#   service_namespace  = aws_appautoscaling_target.worker.service_namespace
#
#   target_tracking_scaling_policy_configuration {
#     target_value       = 70.0
#     predefined_metric_specification {
#       predefined_metric_type = "ECSServiceAverageCPUUtilization"
#     }
#     scale_in_cooldown  = 60
#     scale_out_cooldown = 60
#   }
# }
