# Architecture

This document provides a detailed reference for every component deployed by this CDK project and explains the decisions behind the design.

---

## Component map

```
AWS Account / Region
│
└── PythonCdkStack (CloudFormation stack)
    │
    ├── VpcConstruct
    │   ├── (option A) ec2.Vpc — new VPC with public + private subnets
    │   └── (option B) ec2.Vpc.from_lookup — reuse existing VPC by ID
    │
    ├── EksConstruct
    │   ├── iam.Role — EKS control-plane role
    │   ├── iam.Role — shared EC2 node role (both managed node groups)
    │   ├── eks.Cluster
    │   │   ├── SystemNodes (managed node group — On-Demand)
    │   │   └── SpotNodes   (managed node group — Spot)
    │   └── KubectlV32Layer — Lambda layer for CDK kubectl provider
    │
    ├── KarpenterConstruct
    │   ├── iam.Role + CfnInstanceProfile — Karpenter node role
    │   ├── sqs.Queue — Spot interruption queue
    │   ├── events.Rule ×4 — EventBridge rules → SQS
    │   ├── iam.Role — Karpenter controller role (IRSA)
    │   ├── Helm chart — karpenter (OCI, public.ecr.aws/karpenter)
    │   ├── EC2NodeClass manifest — default node class
    │   └── NodePool manifest — default node pool
    │
    └── AddonsConstruct
        ├── iam.Role — ALB Controller role (IRSA)
        ├── Helm chart — aws-load-balancer-controller
        ├── iam.Role — EBS CSI Driver role (IRSA)
        ├── eks.CfnAddon — aws-ebs-csi-driver
        └── (optional)
            ├── iam.Role — ExternalDNS role (IRSA)
            └── Helm chart — external-dns
```

---

## Networking

### VPC modes

| Mode | How to activate | What happens |
|---|---|---|
| **Create new** | Omit `vpc_id` from context | A new VPC is created with one public and one private subnet per AZ (default 3 AZs). `nat_gateways=1` keeps cost low; increase if higher egress availability is needed. |
| **Reuse existing** | Set `vpc_id=<id>` in context | `ec2.Vpc.from_lookup` is called. Subnet tags **must** already exist (see below). |

### Required subnet tags (existing VPC)

The EKS control plane, the ALB Controller, and Karpenter all rely on subnet tags to discover the correct subnets automatically. New VPCs are tagged by the construct; for existing VPCs these must be applied before deploying.

| Subnet type | Tag key | Tag value |
|---|---|---|
| Public | `kubernetes.io/role/elb` | `1` |
| Private | `kubernetes.io/role/internal-elb` | `1` |
| Both | `kubernetes.io/cluster/<cluster_name>` | `owned` or `shared` |
| Both | `karpenter.sh/discovery` | `<cluster_name>` |

---

## EKS cluster

### Endpoints

The control plane exposes both a **public endpoint** (for `kubectl` from developer machines) and a **private endpoint** (for in-cluster API server access). You can tighten this to private-only after verifying in-cluster tooling works, by changing `endpoint_access` in `EksConstruct`.

### Node groups

Two **managed node groups** (MNG) are created alongside Karpenter so that the cluster is functional even before Karpenter provisions its first node.

#### System node group (On-Demand)

| Property | Value |
|---|---|
| Capacity type | `ON_DEMAND` |
| Default instance types | `m5.large`, `m5a.large`, `m6i.large` |
| Default size | min 2 / desired 2 / max 4 |
| Taint | `CriticalAddonsOnly=true:NoSchedule` |
| Node label | `role=system` |

The `CriticalAddonsOnly` taint means only pods that explicitly tolerate it will land here — principally `kube-system` DaemonSets and the Karpenter controller. This makes the cluster's critical control-plane components immune to Spot interruptions.

#### Spot node group (general workloads)

| Property | Value |
|---|---|
| Capacity type | `SPOT` |
| Default instance types | 10 types across m5/m6, c5/c6, r5 families |
| Default size | min 1 / desired 2 / max 10 |
| Node label | `role=spot-worker` |

Multiple instance types are specified so EC2 can draw from a larger capacity pool, reducing the likelihood of interruptions.

### IAM roles

| Role | Purpose |
|---|---|
| `EksClusterRole` | EKS service role; allows `eks.amazonaws.com` to manage cluster resources |
| `EksNodeRole-<name>` | EC2 instance role for both managed node groups; carries `AmazonEKSWorkerNodePolicy`, `AmazonEC2ContainerRegistryReadOnly`, `AmazonEKS_CNI_Policy`, `AmazonSSMManagedInstanceCore` |

---

## Karpenter

Karpenter v1.x uses the new `NodePool` / `EC2NodeClass` API (superseding `Provisioner` / `AWSNodeTemplate` from v0.x).

### Interruption handling

Karpenter monitors a dedicated **SQS queue** for EC2 lifecycle events and cordon/drains nodes before they are reclaimed:

| EventBridge rule | Source | Detail type |
|---|---|---|
| `SpotInterruptionRule` | `aws.ec2` | `EC2 Spot Instance Interruption Warning` |
| `RebalanceRule` | `aws.ec2` | `EC2 Instance Rebalance Recommendation` |
| `InstanceStateChangeRule` | `aws.ec2` | `EC2 Instance State-change Notification` |
| `ScheduledChangeRule` | `aws.health` | `AWS Health Event` |

### Controller IAM (IRSA)

The Karpenter controller authenticates to AWS via **IAM Roles for Service Accounts (IRSA)** using an OIDC identity provider attached to the cluster. `CfnJson` is used to correctly embed the OIDC issuer URL (a CloudFormation token) as a map key inside the trust policy condition block.

The controller role grants only scoped permissions:
- Launch / terminate EC2 instances and fleets tagged with the cluster name
- Read EC2 instance types, images, subnets, and Spot price history
- Send/receive messages on the interruption SQS queue
- Pass the node IAM role to EC2
- Create/delete instance profiles scoped to the cluster

### Default NodePool

```
capacity-type : spot → on-demand (fallback)
instance categories : c, m, r
instance generation : > 2
excluded sizes      : nano, micro, small
cpu limit           : 1000 vCPU
memory limit        : 2000 Gi
consolidation       : WhenEmptyOrUnderutilized, after 1 minute
```

See [karpenter.md](karpenter.md) for guidance on customising NodePools.

---

## Cluster addons

### AWS Load Balancer Controller

Watches `Ingress` (class `alb`) and `Service` (type `LoadBalancer`, annotation `service.beta.kubernetes.io/aws-load-balancer-type: external`) resources and provisions Application Load Balancers and Network Load Balancers respectively.

| Property | Value |
|---|---|
| Helm chart | `aws-load-balancer-controller` from `https://aws.github.io/eks-charts` |
| Namespace | `kube-system` |
| Replicas | 2 |
| IAM | IRSA (`AWSLoadBalancerControllerRole-<name>`) |
| Scheduling | Toleration for `CriticalAddonsOnly`; runs on system nodes |

### EBS CSI Driver

Installed as a **managed EKS addon** (`aws-ebs-csi-driver`) with an IRSA role. Enables `PersistentVolumeClaim` resources backed by EBS gp3 volumes with encryption at rest.

To use it, set `storageClassName: gp3` in your `PersistentVolumeClaim`.

### ExternalDNS (optional)

When `install_external_dns=true`, ExternalDNS watches `Ingress` and `Service` resources and creates/updates Route 53 records automatically. Scoped to the hosted zones listed in `hosted_zone_arns`.

---

## Security design highlights

- All EKS nodes run in **private subnets** only.
- The `CriticalAddonsOnly` taint isolates system-level workloads from application workloads.
- All IRSA trust policies use **least-privilege condition blocks** (`StringEquals` on both `:aud` and `:sub`) to prevent cross-service-account privilege escalation.
- EBS volumes provisioned by the CSI driver are **encrypted by default** (gp3, AES-256).
- Karpenter node instance profiles are scoped to the cluster via resource tags.
- The interruption SQS queue uses **SQS-managed encryption**.
