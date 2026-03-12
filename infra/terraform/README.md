# pywrkr — Terraform Infrastructure

Terraform configuration for deploying pywrkr (distributed load testing) on AWS ECS Fargate.

## Architecture

A master/worker topology deployed across these modules:

| Module | Purpose |
|---|---|
| `network` | VPC, subnets (public/private), optional NAT gateway |
| `iam` | ECS task execution and task roles |
| `ecr` | Container registry for pywrkr images |
| `ecs-cluster` | ECS Fargate cluster |
| `ecs-service-pywrkr` | Master + worker ECS services with Cloud Map service discovery |
| `cloudwatch` | Log groups for master and worker containers |

## Prerequisites

- Terraform >= 1.6
- AWS CLI configured with appropriate credentials
- AWS provider ~> 5.0

## Quick Start

```bash
cp terraform.tfvars.example terraform.tfvars
# Edit terraform.tfvars with your target URL and settings

terraform init
terraform plan
terraform apply
```

## Key Variables

| Variable | Description | Default |
|---|---|---|
| `target_url` | URL to load test | `https://example.com` |
| `worker_count` | Number of worker tasks | `3` |
| `connections` | Concurrent connections per worker | `100` |
| `users` | Virtual users (0 = connection mode) | `0` |
| `test_duration` | Test duration in seconds | `300` |
| `enable_nat_gateway` | Use private subnets (~$32/mo per AZ) | `false` |

See [terraform.tfvars.example](terraform.tfvars.example) for all options including rate limiting, SLO thresholds, scenarios, and observability endpoints.

## Remote State

To enable remote state, uncomment the S3 backend block in `versions.tf` and configure your bucket/DynamoDB table.
