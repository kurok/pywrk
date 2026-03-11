# pywrkr Infrastructure — ECS Fargate + Jenkins

Production-grade infrastructure for running distributed pywrkr load tests on AWS ECS Fargate, orchestrated by Jenkins.

## Architecture

```
┌─────────────┐     ┌──────────────────────────────────────────────────┐
│   Jenkins    │     │                  AWS VPC                         │
│             │────▶│  ┌─────────────┐   Cloud Map DNS                 │
│  Pipeline    │     │  │   Master    │◀── pywrkr-master.pywrkr.local  │
│  - build     │     │  │  (Fargate)  │                                │
│  - deploy    │     │  │  :9000      │                                │
│  - test      │     │  └──────┬──────┘                                │
│  - logs      │     │         │ TCP 9000                              │
│              │     │  ┌──────┼──────┐                                │
│              │     │  │Worker│Worker│Worker│  (Fargate × N)          │
│              │     │  └──────┴──────┴──────┘                         │
│              │     │         │                                        │
│              │     │         ▼ HTTPS                                  │
│              │     │   Target Website                                 │
└─────────────┘     └──────────────────────────────────────────────────┘
         │
         └──▶ ECR (Docker images)
         └──▶ CloudWatch (logs)
```

### Components

| Module | Purpose |
|--------|---------|
| `network` | VPC, 2 public + 2 private subnets, IGW, optional NAT |
| `iam` | Task execution role + task role with least-privilege |
| `ecr` | Container registry with lifecycle cleanup |
| `ecs-cluster` | Fargate cluster with Spot capacity provider |
| `ecs-service-pywrkr` | Master + worker services, Cloud Map, security groups |
| `cloudwatch` | Log groups for master and workers |

## Prerequisites

- **AWS account** with permissions to create VPC, ECS, ECR, IAM, Cloud Map resources
- **Terraform** >= 1.6
- **AWS CLI** v2
- **Docker** (for building images)
- **Jenkins** with:
  - AWS credentials plugin (or IAM instance role)
  - Docker pipeline plugin
  - Terraform installed on agent

## Quick Start

### 1. Bootstrap infrastructure

```bash
cd infra/terraform

# Copy and edit the example vars
cp terraform.tfvars.example terraform.tfvars
# Edit terraform.tfvars with your settings

terraform init
terraform plan
terraform apply
```

### 2. Build and push the Docker image

```bash
# Get ECR login
ECR_URL=$(terraform output -raw ecr_repository_url)
aws ecr get-login-password --region us-east-1 | docker login --username AWS --password-stdin $ECR_URL

# Build from repo root
cd ../..
docker build -f infra/docker/Dockerfile -t pywrkr:latest .
docker tag pywrkr:latest $ECR_URL:latest
docker push $ECR_URL:latest
```

### 3. Run via Jenkins

1. Create a new Pipeline job pointing to `infra/jenkins/Jenkinsfile`
2. Configure parameters (or use defaults)
3. Click **Build with Parameters**

### 4. Run manually (without Jenkins)

```bash
cd infra/terraform

# Update target URL and trigger fresh deployment
terraform apply -var 'target_url=https://your-site.com' -var 'test_duration=300'

# Force new deployment to start a fresh test
CLUSTER=$(terraform output -raw ecs_cluster_name)
MASTER=$(terraform output -raw master_service_name)
WORKER=$(terraform output -raw worker_service_name)

aws ecs update-service --cluster $CLUSTER --service $MASTER --force-new-deployment
aws ecs update-service --cluster $CLUSTER --service $WORKER --force-new-deployment
```

## Jenkins Parameters

| Parameter | Default | Description |
|-----------|---------|-------------|
| `AWS_REGION` | us-east-1 | AWS region |
| `ENVIRONMENT` | dev | Environment name |
| `IMAGE_TAG` | latest | Docker image tag |
| `TARGET_URL` | https://example.com | Target URL |
| `TEST_DURATION` | 300 | Duration in seconds |
| `USERS` | 0 | Virtual users (0 = connection mode) |
| `CONNECTIONS` | 100 | Concurrent connections per worker |
| `RATE` | 0 | Target RPS (0 = unlimited) |
| `WORKER_COUNT` | 3 | Number of worker containers |
| `THRESHOLDS` | (empty) | Comma-separated SLO thresholds |
| `SCENARIO_FILE` | (empty) | Scenario file path in container |
| `CLEANUP_AFTER_RUN` | false | Scale to 0 after test |
| `DESTROY_INFRA` | false | Destroy all resources after test |

## Examples

### 5-minute test against example.com

```bash
terraform apply \
  -var 'target_url=https://example.com' \
  -var 'test_duration=300' \
  -var 'connections=100' \
  -var 'worker_count=3'
```

### Scenario-driven API test with thresholds

```bash
terraform apply \
  -var 'scenario_file=/scenarios/api-test.json' \
  -var 'test_duration=600' \
  -var 'users=500' \
  -var 'worker_count=5' \
  -var 'thresholds=["p95 < 500ms", "error_rate < 1%"]'
```

### User simulation mode

```bash
terraform apply \
  -var 'target_url=https://your-api.com' \
  -var 'users=1000' \
  -var 'test_duration=600' \
  -var 'worker_count=10'
```

## Inspecting CloudWatch Logs

```bash
# Get log group names
terraform output master_log_group
terraform output worker_log_group

# Stream master logs live
aws logs tail "/ecs/pywrkr-dev/master" --follow --region us-east-1

# Get last hour of worker logs
aws logs filter-log-events \
  --log-group-name "/ecs/pywrkr-dev/worker" \
  --start-time $(( $(date +%s) - 3600 ))000 \
  --output text --query 'events[*].message'
```

## Scaling Workers

```bash
# Via Terraform (persisted)
terraform apply -var 'worker_count=10'

# Via AWS CLI (ephemeral, resets on next terraform apply)
aws ecs update-service \
  --cluster pywrkr-dev-cluster \
  --service pywrkr-dev-worker \
  --desired-count 10
```

## Destroying Everything

```bash
cd infra/terraform
terraform destroy
```

Or in Jenkins: set `DESTROY_INFRA=true` when running the pipeline.

## Cost Notes

### ECS Fargate pricing (us-east-1, on-demand)

| Resource | Per-task cost | 4 tasks (1M + 3W) |
|----------|-------------|---------------------|
| 1 vCPU   | $0.04048/hr | $0.162/hr          |
| 2 GB RAM | $0.004445/hr × 2 | $0.036/hr    |
| **Total** | ~$0.049/hr  | **~$0.198/hr**     |

**A 30-minute test with 4 tasks costs approximately $0.10.**

### Fargate Spot

The cluster defaults to `FARGATE_SPOT` capacity provider, which provides up to 70% discount. Spot tasks may be interrupted, which is acceptable for load testing.

### NAT Gateway

NAT gateway costs ~$32/month + data processing fees. Disabled by default — tasks run in public subnets with `assign_public_ip = true`. Enable `enable_nat_gateway = true` if security policy requires private subnets.

### Scale-to-zero

Set `CLEANUP_AFTER_RUN=true` in Jenkins to scale services to 0 after each test. This stops all Fargate billing. The VPC and cluster resources have no ongoing compute cost.

### ECS/Fargate vs EKS

| | ECS/Fargate | EKS |
|--|-------------|-----|
| **Control plane** | Free | $0.10/hr ($73/mo) |
| **Node management** | Managed | Self-managed or Fargate |
| **Complexity** | Low | High |
| **Best for** | < 20 workers | > 20 workers, Karpenter |

**Recommendation**: Use ECS/Fargate for up to ~20 workers. Beyond that, consider EKS + Karpenter for more efficient bin-packing and faster scaling.

## ECS Task Entrypoint Strategy

The Docker image uses `ENTRYPOINT ["pywrkr"]` and the ECS task definition passes the CLI arguments via the `command` field:

- **Master**: `--master --expect-workers 3 --bind 0.0.0.0 --port 9000 -d 300 -c 100 https://example.com`
- **Worker**: `--worker pywrkr-master.pywrkr.local:9000`

Workers discover the master via Cloud Map DNS: `pywrkr-master.pywrkr.local` resolves to the master task's private IP.

## Future Improvements

### EKS + Karpenter (50+ workers)

For high-scale tests requiring many workers:

1. **EKS cluster** with Karpenter for just-in-time node provisioning
2. **Karpenter NodePool** configured for compute-optimized instances (c6i, c7g)
3. **Kubernetes Jobs** instead of ECS services — natural fit for batch workloads
4. **Horizontal Pod Autoscaler** for dynamic worker scaling
5. **Pod topology spread** constraints for cross-AZ distribution
6. **Graviton (ARM64)** instances for ~20% cost savings

### Other improvements

- **S3 artifact storage**: Upload JSON/HTML reports to S3 instead of CloudWatch
- **Grafana dashboard**: Pre-built dashboard for pywrkr OpenTelemetry metrics
- **Slack/PagerDuty notifications**: Alert on threshold breaches
- **Terraform remote state**: S3 + DynamoDB backend for team collaboration
- **GitHub Actions alternative**: Replace Jenkins with a GitHub Actions workflow
- **Multi-region testing**: Deploy workers in multiple regions for geo-distributed load
