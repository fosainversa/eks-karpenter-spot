# EKS Platform — CDK Python

Production-grade Amazon EKS cluster with **Spot instances**, **Karpenter** autoscaling, and all the supporting AWS services needed to expose applications, built with AWS CDK for Python.

---

## Table of contents

- [Architecture overview](#architecture-overview)
- [Repository layout](#repository-layout)
- [Quick start](#quick-start)
- [Configuration reference](#configuration-reference)
- [Useful CDK commands](#useful-cdk-commands)
- [Additional documentation](#additional-documentation)

---

## Architecture overview

```
┌─────────────────────────────────────────────────────────────────────┐
│  VPC  (new or existing)                                             │
│                                                                     │
│  ┌──────────────────┐        ┌──────────────────────────────────┐  │
│  │  Public subnets  │        │  Private subnets                 │  │
│  │  (ALB, NAT GW)   │        │  (EKS nodes, control-plane ENIs) │  │
│  └──────────────────┘        └──────────────────────────────────┘  │
└─────────────────────────────────────────────────────────────────────┘
                                        │
                              ┌─────────▼──────────┐
                              │   EKS Control Plane │
                              │   (public + private │
                              │    endpoint)        │
                              └─────────┬──────────┘
                    ┌───────────────────┼──────────────────────┐
                    │                   │                      │
          ┌─────────▼────────┐  ┌───────▼──────────┐  ┌──────▼──────────┐
          │  System MNG       │  │  Spot MNG         │  │  Karpenter      │
          │  On-Demand        │  │  Spot instances   │  │  dynamic nodes  │
          │  (kube-system,    │  │  (general         │  │  (spot-first,   │
          │   Karpenter ctrl) │  │   workloads)      │  │   c/m/r family) │
          └──────────────────┘  └──────────────────┘  └─────────────────┘
                                        │
              ┌─────────────────────────┼──────────────────────┐
              │                         │                      │
   ┌──────────▼──────────┐  ┌──────────▼──────────┐  ┌────────▼────────┐
   │  AWS Load Balancer  │  │  EBS CSI Driver      │  │  ExternalDNS    │
   │  Controller (ALB /  │  │  (gp3 PVCs)          │  │  (optional,     │
   │  NLB for Ingress)   │  │                      │  │   Route 53)     │
   └─────────────────────┘  └─────────────────────┘  └─────────────────┘
```

| Layer | Component | Purpose |
|---|---|---|
| **Networking** | VPC construct | New VPC or lookup of an existing one; subnets auto-tagged for EKS and Karpenter discovery |
| **Compute** | System managed node group | On-Demand instances tainted `CriticalAddonsOnly`; runs kube-system and Karpenter controller |
| **Compute** | Spot managed node group | Spot instances for general, non-critical workloads |
| **Autoscaling** | Karpenter | Dynamic, cost-efficient node provisioning; SQS interruption queue + EventBridge rules for graceful Spot handling |
| **Ingress** | AWS Load Balancer Controller | Provisions ALBs and NLBs from `Ingress` / `Service` resources via IRSA |
| **Storage** | EBS CSI Driver | Managed EKS addon; enables `PersistentVolumeClaim` backed by encrypted gp3 volumes |
| **DNS** | ExternalDNS (optional) | Automatically manages Route 53 records for exposed services |

For a deeper dive see [docs/architecture.md](docs/architecture.md).

---

## Repository layout

```
.
├── app.py                        # CDK app entry point
├── cdk.json                      # CDK context & feature flags
├── requirements.txt              # Python dependencies
├── requirements-dev.txt          # Dev/test dependencies
├── python_cdk/
│   ├── python_cdk_stack.py       # Main stack – wires all constructs together
│   └── constructs/
│       ├── vpc_construct.py      # VPC (new or existing)
│       ├── eks_construct.py      # EKS cluster + managed node groups
│       ├── karpenter_construct.py# Karpenter installation & configuration
│       └── addons_construct.py   # ALB Controller, EBS CSI, ExternalDNS
├── tests/
│   └── unit/
│       └── test_python_cdk_stack.py
└── docs/
    ├── architecture.md           # Detailed architecture reference
    ├── deploying.md              # Step-by-step deployment guide
    └── karpenter.md              # Karpenter NodePool & EC2NodeClass guide
```

---

## Quick start

### Prerequisites

| Tool | Minimum version |
|---|---|
| Python | 3.9 |
| Node.js | 20 or 22 |
| AWS CDK CLI | 2.x (`npm install -g aws-cdk`) |
| AWS CLI | 2.x, configured with credentials |

### 1 — Set up the Python environment

```bash
python3 -m venv .venv
source .venv/bin/activate          # macOS / Linux
# .venv\Scripts\activate.bat       # Windows

pip install -r requirements.txt
```

### 2 — Bootstrap (first time per account/region)

```bash
cdk bootstrap aws://<ACCOUNT_ID>/<REGION>
```

### 3 — Deploy

```bash
# Create a brand-new VPC:
cdk deploy

# Reuse an existing VPC:
cdk deploy --context vpc_id=vpc-0abc123456

# Full example:
cdk deploy \
  --context cluster_name=my-eks \
  --context vpc_id=vpc-0abc123456 \
  --context kubernetes_version=1.32 \
  --context karpenter_version=1.0.6 \
  --context install_external_dns=true \
  --context hosted_zone_arns='["arn:aws:route53:::hostedzone/XXXX"]'
```

See [docs/deploying.md](docs/deploying.md) for the full step-by-step guide.

---

## Configuration reference

All options are set in `cdk.json` under `"context"` or passed with `--context` at deploy time. CLI flags take precedence over `cdk.json`.

| Key | Type | Default | Description |
|---|---|---|---|
| `cluster_name` | string | `eks-cluster` | EKS cluster name — also used to name IAM roles and discovery tags |
| `vpc_id` | string | *(empty — create new)* | Reuse this existing VPC ID instead of creating one |
| `vpc_cidr` | string | `10.0.0.0/16` | CIDR for the new VPC (ignored when `vpc_id` is set) |
| `kubernetes_version` | string | `1.32` | Kubernetes version |
| `system_instance_types` | list | `["m5.large","m5a.large","m6i.large"]` | On-Demand instance types for the system node group |
| `spot_instance_types` | list | `["m5.large","m5a.large",…]` | Spot instance types for the general node group |
| `karpenter_version` | string | `1.0.6` | Karpenter Helm chart version |
| `install_external_dns` | bool | `false` | Install the ExternalDNS addon |
| `hosted_zone_arns` | list | `["arn:aws:route53:::hostedzone/*"]` | Route 53 hosted zone ARNs granted to ExternalDNS |

---

## Useful CDK commands

```bash
cdk ls          # List all stacks
cdk synth       # Synthesize CloudFormation template
cdk deploy      # Deploy the stack
cdk diff        # Compare deployed stack with local changes
cdk destroy     # Tear down all resources
cdk docs        # Open CDK documentation
pytest          # Run unit tests
```

---

## Additional documentation

| Document | Description |
|---|---|
| [docs/architecture.md](docs/architecture.md) | In-depth architecture decisions, IAM design, and component interactions |
| [docs/deploying.md](docs/deploying.md) | Step-by-step deployment guide including VPC, kubectl access, and verification |
| [docs/karpenter.md](docs/karpenter.md) | Karpenter NodePool and EC2NodeClass configuration guide |
