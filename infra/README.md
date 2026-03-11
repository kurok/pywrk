# pywrkr Infrastructure — Distributed Load Testing on AWS ECS Fargate

Complete infrastructure for running distributed pywrkr load tests at scale on AWS ECS Fargate, with Jenkins CI/CD orchestration and interactive HTML reporting.

---

## Table of Contents

- [Architecture Overview](#architecture-overview)
- [How It Works](#how-it-works)
- [Prerequisites](#prerequisites)
- [AWS IAM Permissions](#aws-iam-permissions)
- [Directory Structure](#directory-structure)
- [Terraform Modules Reference](#terraform-modules-reference)
- [Getting Started — Manual (Terraform CLI)](#getting-started--manual-terraform-cli)
- [Getting Started — Jenkins Pipeline](#getting-started--jenkins-pipeline)
- [Configuration Reference](#configuration-reference)
- [Test Modes](#test-modes)
- [Scenarios](#scenarios)
- [HTML Reporting](#html-reporting)
- [Observability](#observability)
- [Inspecting Logs](#inspecting-logs)
- [Debugging with ECS Exec](#debugging-with-ecs-exec)
- [Scaling Workers](#scaling-workers)
- [Cleanup and Teardown](#cleanup-and-teardown)
- [Cost Reference](#cost-reference)
- [Security Considerations](#security-considerations)
- [Troubleshooting](#troubleshooting)
- [Future Improvements](#future-improvements)

---

## Architecture Overview

```
┌──────────────────┐      ┌─────────────────────────────────────────────────────────┐
│     Jenkins      │      │                     AWS VPC (10.0.0.0/16)              │
│                  │      │                                                         │
│  1. Checkout     │      │   Public Subnet AZ-a          Public Subnet AZ-b       │
│  2. Terraform    │─────▶│   ┌──────────────┐            ┌──────────────┐         │
│  3. Deploy       │      │   │   Master     │            │              │         │
│  4. Poll CW logs │      │   │  (Fargate)   │◀─── Cloud Map DNS         │         │
│  5. HTML report  │      │   │  TCP :9000   │    pywrkr-master          │         │
│  6. Cleanup      │      │   └──────┬───────┘    .pywrkr.local          │         │
│                  │      │          │ TCP 9000                           │         │
│                  │      │   ┌──────┼───────┐    ┌──────────────┐       │         │
│                  │      │   │Worker│Worker │    │   Worker     │       │         │
│                  │      │   │  #1  │  #2   │    │    #N        │       │         │
│                  │      │   │(Farg)│(Farg) │    │  (Fargate)   │       │         │
│                  │      │   └──────┴───────┘    └──────────────┘       │         │
│                  │      │          │                     │              │         │
│                  │      │          ▼ HTTP/HTTPS          ▼              │         │
│                  │      │     ┌─────────────────────────────┐          │         │
│                  │      │     │       Target Website        │          │         │
└──────────────────┘      │     └─────────────────────────────┘          │         │
         │                └─────────────────────────────────────────────────────────┘
         │
         ├──▶ CloudWatch (master + worker logs, JSON results)
         ├──▶ ECR (container registry — optional, GHCR used by default)
         └──▶ HTML Report (published as Jenkins build artifact)
```

### Components

| Component | AWS Service | Purpose |
|-----------|-------------|---------|
| VPC | Amazon VPC | Isolated network with 2 public + 2 private subnets across 2 AZs |
| ECS Cluster | Amazon ECS | Fargate cluster with FARGATE_SPOT capacity provider |
| Master Service | ECS Fargate | Single task that coordinates workers and collects results |
| Worker Services | ECS Fargate | N tasks that generate HTTP load against the target |
| Service Discovery | AWS Cloud Map | Private DNS (`pywrkr-master.pywrkr.local`) for worker-to-master connectivity |
| Logging | CloudWatch Logs | Centralized log groups for master and worker containers |
| Container Registry | Amazon ECR | Optional local registry (GHCR image used by default) |
| IAM Roles | AWS IAM | Task execution role (image pull, logs) + task role (runtime permissions) |
| Security Groups | EC2 SGs | Network-level isolation between master, workers, and internet |

---

## How It Works

### Distributed Mode — Master/Worker Protocol

pywrkr uses a master/worker architecture for distributed load testing:

1. **Master starts** and listens on TCP port 9000, waiting for N workers to connect
2. **Workers connect** to the master via Cloud Map DNS (`pywrkr-master.pywrkr.local:9000`)
3. **Master distributes config** — target URL, duration, connections, thresholds, etc.
4. **Workers execute** the benchmark concurrently, each generating independent HTTP load
5. **Workers report results** back to the master upon completion
6. **Master merges** all worker statistics and produces a unified report
7. **JSON results** are written to `/tmp/results.json` and echoed to stdout with markers (`---PYWRKR_JSON_START---` / `---PYWRKR_JSON_END---`) so Jenkins can extract them from CloudWatch Logs

### Jenkins Pipeline Flow

```
Checkout ─▶ Verify AWS ─▶ Prepare Vars ─▶ Terraform Init ─▶ Plan ─▶ Apply
                                                                        │
     ┌──────────────────────────────────────────────────────────────────┘
     ▼
  Run Load Test ─▶ Poll CloudWatch ─▶ Extract JSON ─▶ Generate HTML ─▶ Publish
     (force new      (wait for           (regex           (Chart.js       (archive +
      deployment)     JSON_END marker)    extraction)      report)         HTML Publisher)
                                                                        │
     ┌──────────────────────────────────────────────────────────────────┘
     ▼
  Cleanup (scale to 0 or terraform destroy)
```

### Master Container Command

The master ECS task overrides the Docker `ENTRYPOINT` with a shell wrapper:

```bash
sh -c "pywrkr --master --expect-workers 2 --bind 0.0.0.0 --port 9000 \
  -d 60 -c 10 https://example.com --json /tmp/results.json ; \
  EXIT_CODE=$?; \
  echo '---PYWRKR_JSON_START---'; \
  cat /tmp/results.json; \
  echo '---PYWRKR_JSON_END---'; \
  exit $EXIT_CODE"
```

This runs the benchmark, captures the exit code (non-zero if thresholds are breached), outputs the JSON results between markers for CloudWatch extraction, then exits with the original exit code.

### Worker Container Command

Workers use the default Docker `ENTRYPOINT ["pywrkr"]` with:

```
--worker pywrkr-master.pywrkr.local:9000
```

Workers discover the master via Cloud Map private DNS, which resolves to the master task's private IP address within the VPC.

---

## Prerequisites

### Software Requirements

| Tool | Minimum Version | Purpose |
|------|----------------|---------|
| Terraform | >= 1.6 | Infrastructure provisioning |
| AWS CLI | v2 | AWS API access, ECS operations, CloudWatch log retrieval |
| Docker | Any recent | Building custom images (optional — GHCR image used by default) |
| Python 3 | >= 3.8 | HTML report generation on Jenkins agent |

### Jenkins Requirements

| Requirement | Details |
|-------------|---------|
| Jenkins | LTS or latest |
| Pipeline plugin | Declarative pipeline support |
| Credentials plugin | Store AWS access keys securely |
| HTML Publisher plugin | Inline HTML report viewing in Jenkins UI |
| Git plugin | SCM checkout from GitHub |
| Timestamps plugin | Console log timestamps |
| Workspace Cleanup plugin | Post-build cleanup |
| Terraform | Installed on the Jenkins agent (available in `$PATH`) |
| AWS CLI v2 | Installed on the Jenkins agent |
| Python 3 | Installed on the Jenkins agent |

### Jenkins Credential Store

Create two **Secret text** credentials in Jenkins:

| Credential ID | Type | Value |
|---------------|------|-------|
| `aws-access-key-id` | Secret text | Your AWS Access Key ID |
| `aws-secret-access-key` | Secret text | Your AWS Secret Access Key |

Navigate to: **Manage Jenkins > Credentials > System > Global > Add Credentials**

---

## AWS IAM Permissions

The IAM user or role running Terraform and the Jenkins pipeline needs the following permissions. Two policy documents are provided: one for the **Terraform operator** (the user/role that runs `terraform apply` and the Jenkins pipeline) and one for the **ECS task roles** (managed automatically by Terraform).

### Option A: AWS Managed Policies (Broadest — Good for Development)

Attach these AWS managed policies to the IAM user/role that runs Terraform and Jenkins:

| Managed Policy | ARN | Purpose |
|---------------|-----|---------|
| **AmazonVPCFullAccess** | `arn:aws:iam::aws:policy/AmazonVPCFullAccess` | VPC, subnets, route tables, IGW, NAT, security groups, elastic IPs |
| **AmazonECS_FullAccess** | `arn:aws:iam::aws:policy/AmazonECS_FullAccess` | ECS clusters, services, task definitions, capacity providers |
| **AmazonEC2ContainerRegistryFullAccess** | `arn:aws:iam::aws:policy/AmazonEC2ContainerRegistryFullAccess` | ECR repositories, image lifecycle |
| **CloudWatchLogsFullAccess** | `arn:aws:iam::aws:policy/CloudWatchLogsFullAccess` | Log groups, log streams, log events |
| **IAMFullAccess** | `arn:aws:iam::aws:policy/IAMFullAccess` | Create/manage IAM roles and policies for ECS tasks |
| **AWSCloudMapFullAccess** | `arn:aws:iam::aws:policy/AWSCloudMapFullAccess` | Cloud Map namespaces and service discovery |

**To attach via AWS CLI:**

```bash
USER_NAME="your-iam-user"

aws iam attach-user-policy --user-name $USER_NAME \
  --policy-arn arn:aws:iam::aws:policy/AmazonVPCFullAccess

aws iam attach-user-policy --user-name $USER_NAME \
  --policy-arn arn:aws:iam::aws:policy/AmazonECS_FullAccess

aws iam attach-user-policy --user-name $USER_NAME \
  --policy-arn arn:aws:iam::aws:policy/AmazonEC2ContainerRegistryFullAccess

aws iam attach-user-policy --user-name $USER_NAME \
  --policy-arn arn:aws:iam::aws:policy/CloudWatchLogsFullAccess

aws iam attach-user-policy --user-name $USER_NAME \
  --policy-arn arn:aws:iam::aws:policy/IAMFullAccess

aws iam attach-user-policy --user-name $USER_NAME \
  --policy-arn arn:aws:iam::aws:policy/AWSCloudMapFullAccess
```

### Option B: Least-Privilege Custom Policy (Recommended for Production)

Create a custom IAM policy with only the permissions this infrastructure actually needs:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "VPCNetworking",
      "Effect": "Allow",
      "Action": [
        "ec2:CreateVpc",
        "ec2:DeleteVpc",
        "ec2:ModifyVpcAttribute",
        "ec2:DescribeVpcs",
        "ec2:DescribeVpcAttribute",
        "ec2:CreateSubnet",
        "ec2:DeleteSubnet",
        "ec2:ModifySubnetAttribute",
        "ec2:DescribeSubnets",
        "ec2:CreateInternetGateway",
        "ec2:DeleteInternetGateway",
        "ec2:AttachInternetGateway",
        "ec2:DetachInternetGateway",
        "ec2:DescribeInternetGateways",
        "ec2:CreateRouteTable",
        "ec2:DeleteRouteTable",
        "ec2:DescribeRouteTables",
        "ec2:CreateRoute",
        "ec2:DeleteRoute",
        "ec2:AssociateRouteTable",
        "ec2:DisassociateRouteTable",
        "ec2:AllocateAddress",
        "ec2:ReleaseAddress",
        "ec2:DescribeAddresses",
        "ec2:CreateNatGateway",
        "ec2:DeleteNatGateway",
        "ec2:DescribeNatGateways",
        "ec2:DescribeAvailabilityZones",
        "ec2:DescribeAccountAttributes",
        "ec2:DescribeNetworkInterfaces"
      ],
      "Resource": "*"
    },
    {
      "Sid": "SecurityGroups",
      "Effect": "Allow",
      "Action": [
        "ec2:CreateSecurityGroup",
        "ec2:DeleteSecurityGroup",
        "ec2:DescribeSecurityGroups",
        "ec2:DescribeSecurityGroupRules",
        "ec2:AuthorizeSecurityGroupIngress",
        "ec2:AuthorizeSecurityGroupEgress",
        "ec2:RevokeSecurityGroupIngress",
        "ec2:RevokeSecurityGroupEgress"
      ],
      "Resource": "*"
    },
    {
      "Sid": "EC2Tags",
      "Effect": "Allow",
      "Action": [
        "ec2:CreateTags",
        "ec2:DeleteTags",
        "ec2:DescribeTags"
      ],
      "Resource": "*"
    },
    {
      "Sid": "ECSClusterAndServices",
      "Effect": "Allow",
      "Action": [
        "ecs:CreateCluster",
        "ecs:DeleteCluster",
        "ecs:DescribeClusters",
        "ecs:PutClusterCapacityProviders",
        "ecs:UpdateCluster",
        "ecs:TagResource",
        "ecs:UntagResource",
        "ecs:RegisterTaskDefinition",
        "ecs:DeregisterTaskDefinition",
        "ecs:DescribeTaskDefinition",
        "ecs:ListTaskDefinitions",
        "ecs:CreateService",
        "ecs:DeleteService",
        "ecs:UpdateService",
        "ecs:DescribeServices",
        "ecs:ListServices",
        "ecs:DescribeTasks",
        "ecs:ListTasks",
        "ecs:RunTask",
        "ecs:StopTask",
        "ecs:ExecuteCommand"
      ],
      "Resource": "*"
    },
    {
      "Sid": "ECR",
      "Effect": "Allow",
      "Action": [
        "ecr:CreateRepository",
        "ecr:DeleteRepository",
        "ecr:DescribeRepositories",
        "ecr:ListTagsForResource",
        "ecr:TagResource",
        "ecr:UntagResource",
        "ecr:PutLifecyclePolicy",
        "ecr:GetLifecyclePolicy",
        "ecr:DeleteLifecyclePolicy",
        "ecr:GetAuthorizationToken",
        "ecr:BatchGetImage",
        "ecr:GetDownloadUrlForLayer"
      ],
      "Resource": "*"
    },
    {
      "Sid": "CloudWatchLogs",
      "Effect": "Allow",
      "Action": [
        "logs:CreateLogGroup",
        "logs:DeleteLogGroup",
        "logs:DescribeLogGroups",
        "logs:PutRetentionPolicy",
        "logs:ListTagsForResource",
        "logs:TagResource",
        "logs:UntagResource",
        "logs:FilterLogEvents",
        "logs:GetLogEvents",
        "logs:DescribeLogStreams"
      ],
      "Resource": "*"
    },
    {
      "Sid": "IAMRolesForECS",
      "Effect": "Allow",
      "Action": [
        "iam:CreateRole",
        "iam:DeleteRole",
        "iam:GetRole",
        "iam:ListRolePolicies",
        "iam:ListAttachedRolePolicies",
        "iam:ListInstanceProfilesForRole",
        "iam:PutRolePolicy",
        "iam:GetRolePolicy",
        "iam:DeleteRolePolicy",
        "iam:AttachRolePolicy",
        "iam:DetachRolePolicy",
        "iam:PassRole",
        "iam:TagRole",
        "iam:UntagRole"
      ],
      "Resource": [
        "arn:aws:iam::*:role/pywrkr-*"
      ]
    },
    {
      "Sid": "IAMReadCallerIdentity",
      "Effect": "Allow",
      "Action": [
        "sts:GetCallerIdentity"
      ],
      "Resource": "*"
    },
    {
      "Sid": "CloudMapServiceDiscovery",
      "Effect": "Allow",
      "Action": [
        "servicediscovery:CreatePrivateDnsNamespace",
        "servicediscovery:DeleteNamespace",
        "servicediscovery:GetNamespace",
        "servicediscovery:ListNamespaces",
        "servicediscovery:GetOperation",
        "servicediscovery:CreateService",
        "servicediscovery:DeleteService",
        "servicediscovery:GetService",
        "servicediscovery:ListServices",
        "servicediscovery:ListTagsForResource",
        "servicediscovery:TagResource",
        "servicediscovery:UntagResource"
      ],
      "Resource": "*"
    },
    {
      "Sid": "Route53ForCloudMap",
      "Effect": "Allow",
      "Action": [
        "route53:CreateHostedZone",
        "route53:DeleteHostedZone",
        "route53:GetHostedZone",
        "route53:ListHostedZones",
        "route53:ChangeResourceRecordSets",
        "route53:ListResourceRecordSets",
        "route53:GetChange"
      ],
      "Resource": "*"
    }
  ]
}
```

**To create and attach:**

```bash
# Save the policy JSON to a file, then:
aws iam create-policy \
  --policy-name pywrkr-infra-operator \
  --policy-document file://pywrkr-policy.json

aws iam attach-user-policy \
  --user-name your-iam-user \
  --policy-arn arn:aws:iam::ACCOUNT_ID:policy/pywrkr-infra-operator
```

### ECS Task Roles (Created Automatically by Terraform)

These roles are created and managed by the `iam` Terraform module. No manual action needed.

**Task Execution Role** (`pywrkr-{env}-ecs-exec`):
- AWS Managed Policy: `AmazonECSTaskExecutionRolePolicy` — allows pulling container images from ECR and writing to CloudWatch Logs

**Task Role** (`pywrkr-{env}-ecs-task`):
- `logs:CreateLogStream`, `logs:PutLogEvents` — write container logs
- `ssmmessages:*` — ECS Exec for debugging (SSH-like access to running containers)

---

## Directory Structure

```
infra/
├── README.md                          ← This file
├── docker/
│   └── Dockerfile                     ← Production image with baked-in scenarios
├── jenkins/
│   ├── Jenkinsfile                    ← Declarative pipeline (10 stages)
│   └── generate_report.py            ← JSON → HTML report generator
├── scenarios/
│   ├── simple-get.json                ← Single-endpoint GET benchmark
│   └── api-test.json                  ← Multi-step API test (4 endpoints)
└── terraform/
    ├── main.tf                        ← Root module — wires all modules together
    ├── variables.tf                   ← All input variables with descriptions
    ├── outputs.tf                     ← Cluster name, service names, log groups, etc.
    ├── versions.tf                    ← Provider requirements (AWS ~> 5.0, TF >= 1.6)
    ├── terraform.tfvars.example       ← Example configuration file
    └── modules/
        ├── network/                   ← VPC, 4 subnets, IGW, optional NAT
        │   ├── main.tf
        │   ├── variables.tf
        │   └── outputs.tf
        ├── iam/                       ← Task execution role + task role
        │   ├── main.tf
        │   ├── variables.tf
        │   └── outputs.tf
        ├── ecr/                       ← Container registry + lifecycle
        │   ├── main.tf
        │   ├── variables.tf
        │   └── outputs.tf
        ├── ecs-cluster/               ← Fargate cluster + capacity providers
        │   ├── main.tf
        │   ├── variables.tf
        │   └── outputs.tf
        ├── ecs-service-pywrkr/        ← Master + worker services, Cloud Map, SGs
        │   ├── main.tf
        │   ├── variables.tf
        │   └── outputs.tf
        └── cloudwatch/                ← Log groups for master + workers
            ├── main.tf
            ├── variables.tf
            └── outputs.tf
```

---

## Terraform Modules Reference

### 1. Network Module (`modules/network`)

Creates an isolated VPC with public and private subnets.

| Resource | Description |
|----------|-------------|
| `aws_vpc` | Main VPC with DNS support and hostnames enabled |
| `aws_internet_gateway` | Internet access for public subnets |
| `aws_subnet.public[0..1]` | 2 public subnets in different AZs (10.0.0.0/24, 10.0.1.0/24) |
| `aws_subnet.private[0..1]` | 2 private subnets in different AZs (10.0.10.0/24, 10.0.11.0/24) |
| `aws_route_table.public` | Route table with 0.0.0.0/0 → Internet Gateway |
| `aws_eip.nat` | Elastic IP for NAT gateway (conditional) |
| `aws_nat_gateway` | NAT gateway in first public subnet (conditional) |
| `aws_route_table.private` | Route table with 0.0.0.0/0 → NAT gateway (conditional) |

**Default behavior:** Tasks run in public subnets with `assign_public_ip = true`. NAT gateway is disabled to save cost. Set `enable_nat_gateway = true` if security policy requires private subnets.

### 2. IAM Module (`modules/iam`)

Creates least-privilege IAM roles for ECS tasks.

| Resource | Description |
|----------|-------------|
| `aws_iam_role.task_execution` | Allows ECS to pull images and write logs |
| `aws_iam_role.task` | Runtime permissions for the container process |
| `aws_iam_role_policy.task` | Inline policy: CloudWatch Logs + ECS Exec (SSM) |

### 3. ECR Module (`modules/ecr`)

Container registry for custom pywrkr images.

| Resource | Description |
|----------|-------------|
| `aws_ecr_repository` | Registry with scan-on-push enabled |
| `aws_ecr_lifecycle_policy` | Auto-cleanup: keeps only the last 10 images |

> **Note:** The default configuration uses the pre-built image from GitHub Container Registry (`ghcr.io/kurok/pywrkr:latest`). ECR is provisioned but not required unless you build custom images.

### 4. ECS Cluster Module (`modules/ecs-cluster`)

| Resource | Description |
|----------|-------------|
| `aws_ecs_cluster` | Fargate cluster with Container Insights and ECS Exec logging |
| `aws_ecs_cluster_capacity_providers` | FARGATE + FARGATE_SPOT providers (SPOT is default) |

### 5. ECS Service Module (`modules/ecs-service-pywrkr`)

The core module — deploys the master and workers.

| Resource | Description |
|----------|-------------|
| `aws_service_discovery_private_dns_namespace` | Private DNS zone (`pywrkr.local`) |
| `aws_service_discovery_service.master` | DNS A record for master (TTL 10s, MULTIVALUE routing) |
| `aws_security_group.master` | Ingress: TCP 9000 from workers. Egress: HTTPS, HTTP, DNS |
| `aws_security_group.worker` | Egress: TCP 9000 (to master), HTTPS, HTTP, DNS. No ingress |
| `aws_ecs_task_definition.master` | Fargate task with shell wrapper, port 9000, JSON output |
| `aws_ecs_task_definition.worker` | Fargate task connecting to master via Cloud Map DNS |
| `aws_ecs_service.master` | Always 1 replica, registered in Cloud Map, force_new_deployment |
| `aws_ecs_service.worker` | N replicas (configurable), depends on master service |

**Security group rules in detail:**

```
Master SG:
  INGRESS: TCP 9000 from Worker SG (coordination protocol)
  EGRESS:  TCP 443  to 0.0.0.0/0 (HTTPS — target, ECR, CloudWatch)
  EGRESS:  TCP 80   to 0.0.0.0/0 (HTTP — target URL)
  EGRESS:  UDP 53   to 0.0.0.0/0 (DNS resolution)
  EGRESS:  TCP 53   to 0.0.0.0/0 (DNS over TCP)

Worker SG:
  INGRESS: (none)
  EGRESS:  TCP 9000 to 0.0.0.0/0 (master coordination)
  EGRESS:  TCP 443  to 0.0.0.0/0 (HTTPS — target, ECR, CloudWatch)
  EGRESS:  TCP 80   to 0.0.0.0/0 (HTTP — target URL)
  EGRESS:  UDP 53   to 0.0.0.0/0 (DNS resolution)
  EGRESS:  TCP 53   to 0.0.0.0/0 (DNS over TCP)
```

### 6. CloudWatch Module (`modules/cloudwatch`)

| Resource | Description |
|----------|-------------|
| `aws_cloudwatch_log_group.master` | `/ecs/{prefix}/master` with configurable retention |
| `aws_cloudwatch_log_group.worker` | `/ecs/{prefix}/worker` with configurable retention |

---

## Getting Started — Manual (Terraform CLI)

### Step 1: Configure AWS credentials

```bash
export AWS_ACCESS_KEY_ID="your-access-key"
export AWS_SECRET_ACCESS_KEY="your-secret-key"
export AWS_DEFAULT_REGION="us-east-1"

# Verify access
aws sts get-caller-identity
```

### Step 2: Configure Terraform variables

```bash
cd infra/terraform

# Copy the example vars
cp terraform.tfvars.example terraform.tfvars
```

Edit `terraform.tfvars`:

```hcl
# --- Required ---
aws_region  = "us-east-1"
environment = "dev"
target_url  = "https://your-target-site.com"

# --- Optional: adjust as needed ---
container_image = "ghcr.io/kurok/pywrkr:latest"
worker_count    = 3
test_duration   = 300
connections     = 100

# Virtual users mode (set users > 0 to enable, disables connection mode)
# users = 500

# Rate limiting (requests per second, 0 = unlimited)
# rate = 1000

# SLO thresholds — test exits non-zero if breached
# thresholds = ["p95 < 500ms", "error_rate < 1%"]

# Scenario mode — path inside the container
# scenario_file = "/scenarios/api-test.json"

# Metadata tags
# tags = {
#   team      = "platform"
#   test_name = "homepage-soak"
# }

# Observability endpoints (optional)
# otel_endpoint     = "https://otel-collector.internal:4318"
# prom_remote_write = "https://pushgateway.internal:9091"

# Cost control: NAT gateway adds ~$32/mo per AZ.
# Set to true if tasks must run in private subnets.
enable_nat_gateway = false

# Task sizing
master_cpu    = 1024
master_memory = 2048
worker_cpu    = 1024
worker_memory = 2048

# Logging
log_retention_days = 14
```

### Step 3: Initialize and deploy

```bash
terraform init
terraform plan
terraform apply
```

Terraform creates all resources (~2-3 minutes). The ECS services will start automatically.

### Step 4: Monitor the test

```bash
# Get resource names
CLUSTER=$(terraform output -raw ecs_cluster_name)
MASTER_LOG=$(terraform output -raw master_log_group)

# Stream master logs live
aws logs tail "$MASTER_LOG" --follow --region us-east-1

# Or get recent logs
aws logs filter-log-events \
  --log-group-name "$MASTER_LOG" \
  --start-time $(( $(date +%s) - 3600 ))000 \
  --region us-east-1 \
  --output text --query 'events[*].message'
```

### Step 5: Trigger a new test run

```bash
CLUSTER=$(terraform output -raw ecs_cluster_name)
MASTER=$(terraform output -raw master_service_name)
WORKER=$(terraform output -raw worker_service_name)

# Force new deployment (restarts all tasks with fresh config)
aws ecs update-service --cluster $CLUSTER --service $MASTER --force-new-deployment --no-cli-pager
aws ecs update-service --cluster $CLUSTER --service $WORKER --force-new-deployment --no-cli-pager
```

### Step 6: Generate HTML report manually

```bash
# Extract JSON from CloudWatch
aws logs filter-log-events \
  --log-group-name "/ecs/pywrkr-dev/master" \
  --start-time $(( $(date +%s) - 3600 ))000 \
  --region us-east-1 \
  --output json \
  --query 'events[*].message' > raw_logs.json

# Extract JSON between markers
python3 -c "
import json, re
messages = json.load(open('raw_logs.json'))
full = chr(10).join(messages)
m = re.search(r'---PYWRKR_JSON_START---\s*(.+?)\s*---PYWRKR_JSON_END---', full, re.DOTALL)
if m:
    data = json.loads(m.group(1))
    json.dump(data, open('results.json', 'w'), indent=2)
    print('Extracted results.json')
else:
    print('JSON markers not found')
"

# Generate HTML report
python3 infra/jenkins/generate_report.py results.json report.html

# Open in browser
open report.html    # macOS
xdg-open report.html  # Linux
```

---

## Getting Started — Jenkins Pipeline

### Step 1: Install Jenkins

```bash
# Docker method (recommended)
docker run -d \
  --name jenkins \
  -p 8080:8080 -p 50000:50000 \
  -v jenkins_home:/var/jenkins_home \
  jenkins/jenkins:lts

# Get initial admin password
docker exec jenkins cat /var/jenkins_home/secrets/initialAdminPassword
```

### Step 2: Install required tools in Jenkins

```bash
# Enter the Jenkins container as root
docker exec -u root -it jenkins bash

# Install Terraform
apt-get update && apt-get install -y unzip python3
curl -fsSL https://releases.hashicorp.com/terraform/1.7.5/terraform_1.7.5_linux_amd64.zip \
  -o /tmp/terraform.zip
unzip /tmp/terraform.zip -d /usr/local/bin/

# Install AWS CLI v2
curl "https://awscli.amazonaws.com/awscli-exe-linux-x86_64.zip" -o /tmp/awscli.zip
unzip /tmp/awscli.zip -d /tmp/
/tmp/aws/install

# Verify
terraform --version
aws --version
python3 --version
```

### Step 3: Configure Jenkins credentials

1. Navigate to **Manage Jenkins > Credentials > System > Global credentials**
2. Add two **Secret text** credentials:
   - ID: `aws-access-key-id` — Value: your AWS Access Key ID
   - ID: `aws-secret-access-key` — Value: your AWS Secret Access Key

### Step 4: Install Jenkins plugins

Navigate to **Manage Jenkins > Plugin Manager > Available** and install:

- **HTML Publisher** — for inline HTML report viewing
- **Pipeline: Stage View** — visual stage progress (usually pre-installed)
- **Timestamps** — console log timestamps

### Step 5: Create the Jenkins job

1. **New Item** > Enter name `pywrkr-loadtest` > Select **Pipeline** > OK
2. Under **Pipeline**:
   - Definition: **Pipeline script from SCM**
   - SCM: **Git**
   - Repository URL: `https://github.com/kurok/pywrkr.git`
   - Branch: `*/feature/ecs-infra` (or `*/main` after merge)
   - Script Path: `infra/jenkins/Jenkinsfile`
3. **Save**

### Step 6: Run the pipeline

1. Click **Build with Parameters**
2. Set parameters (or use defaults):
   - `TARGET_URL` = your target URL
   - `TEST_DURATION` = test duration in seconds
   - `WORKER_COUNT` = number of workers
3. Click **Build**

> **Note:** The first build discovers pipeline parameters from the Jenkinsfile. If parameters appear as null/empty on the first run, the pipeline handles this gracefully with default values. Subsequent builds will show the parameter form correctly.

### Step 7: View the report

After a successful build:
- **pywrkr Load Test Report** link appears in the build sidebar (HTML Publisher)
- **Build Artifacts** contains `pywrkr-report/report.html` and `pywrkr-report/results.json`

---

## Configuration Reference

### Terraform Variables

| Variable | Type | Default | Description |
|----------|------|---------|-------------|
| `aws_region` | string | `us-east-1` | AWS region for all resources |
| `environment` | string | `dev` | Environment name (used in resource naming) |
| `project_name` | string | `pywrkr` | Project name prefix for all resources |
| `vpc_cidr` | string | `10.0.0.0/16` | VPC CIDR block |
| `enable_nat_gateway` | bool | `false` | Enable NAT for private subnets ($32/mo) |
| `container_image` | string | `ghcr.io/kurok/pywrkr:latest` | Full container image URL |
| `master_cpu` | number | `1024` | Master task CPU units (1024 = 1 vCPU) |
| `master_memory` | number | `2048` | Master task memory in MiB |
| `worker_cpu` | number | `1024` | Worker task CPU units |
| `worker_memory` | number | `2048` | Worker task memory in MiB |
| `worker_count` | number | `3` | Number of worker tasks |
| `target_url` | string | `https://example.com` | Target URL for the load test |
| `test_duration` | number | `300` | Test duration in seconds |
| `connections` | number | `100` | Concurrent connections per worker |
| `users` | number | `0` | Virtual users (0 = connection mode) |
| `rate` | number | `0` | Target RPS per worker (0 = unlimited) |
| `thresholds` | list(string) | `[]` | SLO threshold expressions |
| `scenario_file` | string | `""` | Scenario file path inside the container |
| `tags` | map(string) | `{}` | Metadata key=value tags |
| `otel_endpoint` | string | `""` | OpenTelemetry collector endpoint |
| `prom_remote_write` | string | `""` | Prometheus pushgateway endpoint |
| `cloudmap_namespace` | string | `pywrkr.local` | Cloud Map private DNS namespace |
| `log_retention_days` | number | `14` | CloudWatch log retention period |

### Jenkins Pipeline Parameters

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `AWS_REGION` | choice | `us-east-1` | AWS region (us-east-1, us-west-2, eu-west-1, ap-southeast-1) |
| `ENVIRONMENT` | choice | `dev` | Environment name (dev, staging, prod) |
| `IMAGE_TAG` | string | `latest` | pywrkr image tag from ghcr.io/kurok/pywrkr |
| `TARGET_URL` | string | `https://example.com` | Target URL for the load test |
| `TEST_DURATION` | string | `60` | Duration in seconds |
| `USERS` | string | `0` | Virtual users (0 = connection mode) |
| `CONNECTIONS` | string | `10` | Concurrent connections per worker |
| `RATE` | string | `0` | Target RPS (0 = unlimited) |
| `WORKER_COUNT` | string | `2` | Number of pywrkr worker containers |
| `THRESHOLDS` | string | (empty) | Comma-separated SLO thresholds |
| `SCENARIO_FILE` | string | (empty) | Scenario file path in container |
| `CLEANUP_AFTER_RUN` | boolean | `true` | Scale services to 0 after test |
| `DESTROY_INFRA` | boolean | `false` | Destroy all Terraform resources after test |

### Terraform Outputs

| Output | Description | Example |
|--------|-------------|---------|
| `ecs_cluster_name` | ECS cluster name | `pywrkr-dev-cluster` |
| `ecs_cluster_arn` | ECS cluster ARN | `arn:aws:ecs:us-east-1:...` |
| `master_service_name` | Master ECS service name | `pywrkr-dev-master` |
| `worker_service_name` | Worker ECS service name | `pywrkr-dev-worker` |
| `cloudmap_namespace_id` | Cloud Map namespace ID | `ns-abc123...` |
| `cloudmap_namespace_name` | Cloud Map namespace name | `pywrkr.local` |
| `master_log_group` | Master CloudWatch log group | `/ecs/pywrkr-dev/master` |
| `worker_log_group` | Worker CloudWatch log group | `/ecs/pywrkr-dev/worker` |
| `ecr_repository_url` | ECR repository URL | `514510751048.dkr.ecr...` |
| `vpc_id` | VPC identifier | `vpc-0eda723151ac86bdf` |

---

## Test Modes

### Connection Mode (default)

Each worker maintains a fixed number of persistent HTTP connections and sends requests as fast as possible (or at the specified rate).

```hcl
connections = 100   # 100 connections per worker
users       = 0     # 0 = connection mode
worker_count = 3    # total: 300 concurrent connections
```

### Virtual Users Mode

Simulates real users with think time between requests. Set `users > 0` to enable.

```hcl
users        = 500   # 500 virtual users distributed across workers
connections  = 100   # ignored when users > 0
worker_count = 5     # 100 virtual users per worker
```

### Rate-Limited Mode

Limits total requests per second across all workers. Can be combined with either connection or user mode.

```hcl
rate         = 1000  # target: 1000 RPS total
connections  = 50    # connections per worker
worker_count = 5     # each worker targets ~200 RPS
```

### Scenario Mode

Executes a multi-step scenario file instead of a single URL. The scenario file must be available inside the container (baked into the Docker image or mounted).

```hcl
scenario_file = "/scenarios/api-test.json"
target_url    = "https://your-api.com"   # base URL for ${TARGET_URL} in scenario
users         = 200
worker_count  = 4
```

---

## Scenarios

Scenario files define multi-step HTTP workflows. They are JSON files placed in `infra/scenarios/` and baked into the Docker image at `/scenarios/`.

### Simple GET (`/scenarios/simple-get.json`)

```json
{
  "name": "Simple GET benchmark",
  "description": "GET test against a single URL",
  "steps": [
    {
      "name": "homepage",
      "method": "GET",
      "url": "${TARGET_URL}",
      "headers": {
        "Accept": "text/html,application/json"
      }
    }
  ]
}
```

### Multi-Step API Test (`/scenarios/api-test.json`)

```json
{
  "name": "API endpoint multi-step test",
  "description": "Tests multiple API endpoints in sequence per iteration",
  "steps": [
    {
      "name": "health_check",
      "method": "GET",
      "url": "${TARGET_URL}/health",
      "headers": { "Accept": "application/json" }
    },
    {
      "name": "list_items",
      "method": "GET",
      "url": "${TARGET_URL}/api/v1/items?limit=10",
      "headers": {
        "Accept": "application/json",
        "Authorization": "Bearer ${API_TOKEN}"
      }
    },
    {
      "name": "create_item",
      "method": "POST",
      "url": "${TARGET_URL}/api/v1/items",
      "headers": {
        "Content-Type": "application/json",
        "Authorization": "Bearer ${API_TOKEN}"
      },
      "body": "{\"name\": \"load-test-item\", \"value\": 42}"
    },
    {
      "name": "get_item",
      "method": "GET",
      "url": "${TARGET_URL}/api/v1/items/1",
      "headers": {
        "Accept": "application/json",
        "Authorization": "Bearer ${API_TOKEN}"
      }
    }
  ]
}
```

### Writing Custom Scenarios

Create a new JSON file in `infra/scenarios/` and rebuild the Docker image, or mount it as a volume in the task definition.

Environment variables available in scenarios:
- `${TARGET_URL}` — the base target URL from Terraform configuration
- `${API_TOKEN}` — custom token (set via container environment variable)

---

## HTML Reporting

### How Reports Are Generated

1. The master container runs pywrkr with `--json /tmp/results.json`
2. After the test, the JSON is echoed to stdout between `---PYWRKR_JSON_START---` and `---PYWRKR_JSON_END---` markers
3. The Jenkins pipeline polls CloudWatch Logs for the end marker
4. JSON is extracted from the logs using regex
5. `generate_report.py` converts the JSON to a self-contained HTML report with Chart.js

### Report Contents

The HTML report includes:

- **KPI Dashboard** — 8 cards showing: Total Requests, Requests/sec, Error Rate, Mean Latency, p50 Latency, p95 Latency, p99 Latency, Throughput
- **Latency Percentile Chart** — Bar chart of p50, p75, p90, p95, p99, p99.9, p99.99
- **Status Code Distribution** — Doughnut chart of HTTP response codes (color-coded: green=2xx, blue=3xx, yellow=4xx, red=5xx)
- **Latency Breakdown Table** — Min, Max, Mean, Median, Stdev, and all percentiles
- **Error Table** — Error types with counts and percentages (shown only when errors exist)
- **Transfer Metrics** — Total bytes transferred, transfer rate, error count

The report is a single self-contained HTML file that loads Chart.js from CDN. It uses a dark theme with responsive layout.

### Generating Reports Manually

```bash
python3 infra/jenkins/generate_report.py results.json report.html
```

Input: any pywrkr JSON results file. Output: interactive HTML report.

---

## Observability

### OpenTelemetry Integration

```hcl
otel_endpoint = "https://otel-collector.internal:4318"
```

When set, the master container gets the `OTEL_EXPORTER_OTLP_ENDPOINT` environment variable. pywrkr exports metrics via OTLP to the specified collector.

### Prometheus Push Gateway

```hcl
prom_remote_write = "https://pushgateway.internal:9091"
```

When set, pywrkr pushes metrics to the Prometheus push gateway endpoint. The master container gets the `PYWRKR_PROM_ENDPOINT` environment variable.

### Container Insights

ECS Container Insights is enabled on the cluster, providing:
- CPU/memory utilization per task
- Network I/O metrics
- Task count and health metrics

View in **CloudWatch > Container Insights > ECS Clusters**.

---

## Inspecting Logs

### Stream logs in real time

```bash
# Master logs
aws logs tail "/ecs/pywrkr-dev/master" --follow --region us-east-1

# Worker logs
aws logs tail "/ecs/pywrkr-dev/worker" --follow --region us-east-1
```

### Query recent logs

```bash
# Last hour of master logs
aws logs filter-log-events \
  --log-group-name "/ecs/pywrkr-dev/master" \
  --start-time $(( $(date +%s) - 3600 ))000 \
  --region us-east-1 \
  --output text --query 'events[*].message'
```

### Search for specific events

```bash
# Find threshold breaches
aws logs filter-log-events \
  --log-group-name "/ecs/pywrkr-dev/master" \
  --filter-pattern "THRESHOLD BREACHED" \
  --region us-east-1 \
  --output text --query 'events[*].message'

# Find JSON results
aws logs filter-log-events \
  --log-group-name "/ecs/pywrkr-dev/master" \
  --filter-pattern "PYWRKR_JSON_START" \
  --region us-east-1 \
  --output json --query 'events[*].message'
```

---

## Debugging with ECS Exec

ECS Exec is enabled on both master and worker services. You can SSH into running containers:

```bash
CLUSTER=$(cd infra/terraform && terraform output -raw ecs_cluster_name)

# List running tasks
aws ecs list-tasks --cluster $CLUSTER --service-name pywrkr-dev-master --region us-east-1

# Exec into the master container
aws ecs execute-command \
  --cluster $CLUSTER \
  --task <task-id> \
  --container pywrkr-master \
  --interactive \
  --command "/bin/sh" \
  --region us-east-1

# Exec into a worker
aws ecs execute-command \
  --cluster $CLUSTER \
  --task <task-id> \
  --container pywrkr-worker \
  --interactive \
  --command "/bin/sh" \
  --region us-east-1
```

> **Prerequisite:** Install the Session Manager plugin for AWS CLI:
> https://docs.aws.amazon.com/systems-manager/latest/userguide/session-manager-working-with-install-plugin.html

---

## Scaling Workers

### Via Terraform (persistent)

```bash
cd infra/terraform
terraform apply -var 'worker_count=10'
```

### Via AWS CLI (ephemeral — resets on next `terraform apply`)

```bash
aws ecs update-service \
  --cluster pywrkr-dev-cluster \
  --service pywrkr-dev-worker \
  --desired-count 10 \
  --region us-east-1
```

### Recommended Worker Sizing

| Test Scale | Workers | CPU/Memory per Worker | Total Connections |
|-----------|---------|----------------------|-------------------|
| Light (smoke test) | 1-2 | 512/1024 | 10-50 |
| Medium (staging) | 3-5 | 1024/2048 | 100-500 |
| Heavy (production) | 5-10 | 1024/2048 | 500-5,000 |
| Extreme (stress test) | 10-20 | 2048/4096 | 5,000-50,000 |

> **Note:** Beyond ~20 workers, consider EKS + Karpenter for better node scheduling and cost efficiency.

---

## Cleanup and Teardown

### Scale to zero (keep infrastructure for next run)

```bash
CLUSTER=$(cd infra/terraform && terraform output -raw ecs_cluster_name)

aws ecs update-service --cluster $CLUSTER --service pywrkr-dev-worker --desired-count 0 --no-cli-pager
aws ecs update-service --cluster $CLUSTER --service pywrkr-dev-master --desired-count 0 --no-cli-pager
```

Or in Jenkins: set `CLEANUP_AFTER_RUN = true` (default).

This stops all Fargate task billing. The VPC, cluster, log groups, and Cloud Map namespace have no ongoing compute cost.

### Destroy all infrastructure

```bash
cd infra/terraform
terraform destroy
```

Or in Jenkins: set `DESTROY_INFRA = true`.

This removes all AWS resources created by Terraform.

---

## Cost Reference

### ECS Fargate Pricing (us-east-1, on-demand)

| Resource | Unit Cost | Per Task (1 vCPU, 2 GB) |
|----------|-----------|------------------------|
| vCPU | $0.04048/hr | $0.04048/hr |
| Memory | $0.004445/hr per GB | $0.00889/hr |
| **Total per task** | | **$0.04937/hr** |

### Example Test Costs

| Scenario | Tasks | Duration | Cost (On-Demand) | Cost (Spot ~70% off) |
|----------|-------|----------|------------------|---------------------|
| Quick smoke test | 1M + 2W = 3 | 5 min | $0.01 | < $0.01 |
| Standard test | 1M + 3W = 4 | 30 min | $0.10 | $0.03 |
| Extended soak test | 1M + 5W = 6 | 2 hours | $0.59 | $0.18 |
| Full stress test | 1M + 10W = 11 | 1 hour | $0.54 | $0.16 |

### Fargate Spot

The cluster defaults to `FARGATE_SPOT` capacity provider, which provides up to 70% discount over on-demand pricing. Spot tasks may be interrupted with a 2-minute warning, which is acceptable for load testing workloads.

### NAT Gateway

| Component | Cost |
|-----------|------|
| NAT Gateway hourly | $0.045/hr (~$32/mo) |
| Data processing | $0.045/GB |

**Disabled by default.** Tasks run in public subnets with `assign_public_ip = true`. Enable `enable_nat_gateway = true` only if security policy requires private subnets.

### Other Costs

| Resource | Cost |
|----------|------|
| VPC, subnets, route tables, IGW | Free |
| ECS cluster (Fargate) | Free (pay only for tasks) |
| Cloud Map namespace | $0.10/mo per namespace |
| CloudWatch Logs ingestion | $0.50/GB |
| CloudWatch Logs storage | $0.03/GB/mo |
| ECR storage | $0.10/GB/mo |
| Elastic IP (for NAT) | Free while attached to NAT, $0.005/hr if idle |

### ECS Fargate vs EKS Comparison

| Aspect | ECS/Fargate | EKS |
|--------|-------------|-----|
| Control plane cost | Free | $0.10/hr ($73/mo) |
| Node management | Fully managed | Self-managed or Fargate |
| Setup complexity | Low | High |
| Scaling speed | ~30-60s | ~10-30s (with Karpenter) |
| Best for | 1-20 workers | 20+ workers |
| Cost optimization | Fargate Spot | Spot instances + Karpenter |

**Recommendation:** Use ECS/Fargate for up to ~20 workers. Beyond that, consider EKS + Karpenter for better bin-packing, faster scaling, and Graviton (ARM64) support.

---

## Security Considerations

### Network Isolation

- Tasks run in VPC subnets with security groups controlling all traffic
- Master accepts inbound only on TCP 9000, only from the worker security group
- Workers have no inbound access at all
- Outbound is limited to HTTPS (443), HTTP (80), and DNS (53)

### IAM Least Privilege

- Task execution role has only image pull + log write permissions (AWS managed policy)
- Task role has only CloudWatch log write + ECS Exec (SSM) permissions
- All roles are scoped to the specific resource prefix

### Credentials

- AWS credentials are stored in Jenkins credential store, never in code
- The Jenkinsfile references credentials by ID: `aws-access-key-id`, `aws-secret-access-key`
- `.gitignore` excludes `*.tfstate`, `*.auto.tfvars`, and `.terraform/`
- No secrets are baked into Docker images

### Container Image

- Default: `ghcr.io/kurok/pywrkr:latest` from GitHub Container Registry (public)
- For private registries, update `container_image` variable and ensure the task execution role has pull permissions
- ECR repository is provisioned with scan-on-push enabled

---

## Troubleshooting

### Tasks crash-loop after test completion

**Expected behavior.** The master container exits after completing the test (exit code 0). ECS detects the exit and restarts the task (since `desired_count = 1`). The Jenkins pipeline does not wait for services-stable — it polls CloudWatch for the `PYWRKR_JSON_END` marker instead. After collecting results, it scales services to 0.

### `services-stable` wait times out

If running the old pipeline version that uses `aws ecs wait services-stable`, the master container exits after the test, causing ECS to restart it endlessly. The wait never succeeds. Update to the latest Jenkinsfile which polls CloudWatch instead.

### Workers cannot connect to master

1. **Check Cloud Map:** `aws servicediscovery list-instances --service-id <svc-id>` — ensure the master's IP is registered
2. **Check security groups:** Master SG must allow ingress on TCP 9000 from the worker SG
3. **Check DNS resolution:** From a worker container (via ECS Exec): `nslookup pywrkr-master.pywrkr.local`
4. **Check timing:** Workers may start before master is registered in Cloud Map. The `depends_on` in Terraform handles this, but rapid scaling might race

### Terraform apply fails with permission error

Check that your IAM user has all required permissions (see [AWS IAM Permissions](#aws-iam-permissions)). Common missing permissions:
- `servicediscovery:*` — for Cloud Map
- `route53:*` — Cloud Map creates Route53 hosted zones internally
- `iam:PassRole` — needed to assign IAM roles to ECS tasks
- `ec2:CreateSecurityGroup` — needed for security group creation

### Jenkins build fails with "python3 not found"

The Jenkins agent needs Python 3 installed. In the Jenkins Docker container:
```bash
docker exec -u root jenkins apt-get update && apt-get install -y python3
```

### JSON markers not found in CloudWatch logs

1. Check that the master task definition uses `entryPoint = ["sh", "-c"]` (not the default image ENTRYPOINT)
2. Check CloudWatch logs for error messages from the master container
3. Ensure `--json /tmp/results.json` is in the master command
4. The test may not have completed yet — increase the poll timeout

### Terraform state conflicts

If you run Terraform from both Jenkins and locally, state conflicts can occur. Use a remote backend (S3 + DynamoDB) for team environments:

```hcl
# Uncomment in versions.tf
backend "s3" {
  bucket         = "my-terraform-state"
  key            = "pywrkr/terraform.tfstate"
  region         = "us-east-1"
  dynamodb_table = "terraform-locks"
  encrypt        = true
}
```

### NAT Gateway costs accumulating

Check `enable_nat_gateway` in your tfvars. If set to `true`, you're paying ~$32/month even when no tests are running. Set to `false` to use public subnets instead.

---

## Future Improvements

### EKS + Karpenter (50+ workers)

For high-scale tests requiring many workers:

1. **EKS cluster** with Karpenter for just-in-time node provisioning
2. **Karpenter NodePool** configured for compute-optimized instances (c6i, c7g)
3. **Kubernetes Jobs** instead of ECS services — natural fit for batch workloads
4. **Horizontal Pod Autoscaler** for dynamic worker scaling
5. **Pod topology spread** constraints for cross-AZ distribution
6. **Graviton (ARM64)** instances for ~20% cost savings

### Other Improvements

- **S3 artifact storage** — upload JSON/HTML reports directly to S3 from the container (requires S3 permissions for the IAM user)
- **Grafana dashboard** — pre-built dashboard for pywrkr OpenTelemetry metrics
- **Slack/PagerDuty notifications** — alert on threshold breaches via Jenkins plugins
- **Terraform remote state** — S3 + DynamoDB backend for team collaboration
- **GitHub Actions alternative** — replace Jenkins with a GitHub Actions workflow
- **Multi-region testing** — deploy workers in multiple regions for geo-distributed load
- **Scheduled runs** — Jenkins cron triggers for nightly performance regression tests
- **Result history** — store results in a database for trend analysis across runs
