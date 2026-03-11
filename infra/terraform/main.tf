locals {
  name_prefix = "${var.project_name}-${var.environment}"
}

# --- Networking ---

module "network" {
  source = "./modules/network"

  name_prefix        = local.name_prefix
  vpc_cidr           = var.vpc_cidr
  enable_nat_gateway = var.enable_nat_gateway
  aws_region         = var.aws_region
}

# --- IAM ---

module "iam" {
  source = "./modules/iam"

  name_prefix = local.name_prefix
  aws_region  = var.aws_region
}

# --- ECR ---

module "ecr" {
  source = "./modules/ecr"

  name_prefix = local.name_prefix
}

# --- CloudWatch ---

module "cloudwatch" {
  source = "./modules/cloudwatch"

  name_prefix    = local.name_prefix
  retention_days = var.log_retention_days
}

# --- ECS Cluster ---

module "ecs_cluster" {
  source = "./modules/ecs-cluster"

  name_prefix = local.name_prefix
}

# --- ECS Services (master + workers) ---

module "ecs_service_pywrkr" {
  source = "./modules/ecs-service-pywrkr"

  name_prefix        = local.name_prefix
  aws_region         = var.aws_region
  cluster_id         = module.ecs_cluster.cluster_id
  vpc_id             = module.network.vpc_id
  subnet_ids         = var.enable_nat_gateway ? module.network.private_subnet_ids : module.network.public_subnet_ids
  assign_public_ip   = !var.enable_nat_gateway
  cloudmap_namespace = var.cloudmap_namespace

  # IAM
  execution_role_arn = module.iam.task_execution_role_arn
  task_role_arn      = module.iam.task_role_arn

  # Image
  ecr_repository_url = module.ecr.repository_url
  image_tag          = var.image_tag

  # Task sizing
  master_cpu    = var.master_cpu
  master_memory = var.master_memory
  worker_cpu    = var.worker_cpu
  worker_memory = var.worker_memory
  worker_count  = var.worker_count

  # Logging
  master_log_group = module.cloudwatch.master_log_group_name
  worker_log_group = module.cloudwatch.worker_log_group_name

  # pywrkr runtime config
  target_url        = var.target_url
  test_duration     = var.test_duration
  connections       = var.connections
  users             = var.users
  rate              = var.rate
  thresholds        = var.thresholds
  scenario_file     = var.scenario_file
  tags              = var.tags
  otel_endpoint     = var.otel_endpoint
  prom_remote_write = var.prom_remote_write
}
