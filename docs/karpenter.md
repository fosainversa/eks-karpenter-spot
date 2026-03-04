# Karpenter configuration guide

This project installs Karpenter with a sensible default `NodePool` and `EC2NodeClass`. This guide explains how to customise them and how to create additional pools for different workload profiles.

---

## Concepts

| Resource | API group | Purpose |
|---|---|---|
| `EC2NodeClass` | `karpenter.k8s.aws/v1` | Defines **how** nodes are launched — AMI, IAM role, subnets, security groups, storage, user data |
| `NodePool` | `karpenter.sh/v1` | Defines **what** nodes look like — instance types, AZs, capacity type, taints, labels, limits, and disruption behaviour |

A `NodePool` references one `EC2NodeClass` (via `nodeClassRef`). You can have multiple `NodePool` objects pointing to the same or different `EC2NodeClass` objects.

---

## Default configuration deployed by this stack

### EC2NodeClass `default`

```yaml
apiVersion: karpenter.k8s.aws/v1
kind: EC2NodeClass
metadata:
  name: default
spec:
  amiSelectorTerms:
    - alias: al2023@latest          # Always uses the latest AL2023 EKS-optimised AMI
  role: KarpenterNodeRole-<cluster> # The IAM role created by KarpenterConstruct
  subnetSelectorTerms:
    - tags:
        karpenter.sh/discovery: <cluster>
  securityGroupSelectorTerms:
    - tags:
        karpenter.sh/discovery: <cluster>
  tags:
    karpenter.sh/discovery: <cluster>
    kubernetes.io/cluster/<cluster>: owned
  blockDeviceMappings:
    - deviceName: /dev/xvda
      ebs:
        volumeSize: 50Gi
        volumeType: gp3
        encrypted: true
```

### NodePool `default`

```yaml
apiVersion: karpenter.sh/v1
kind: NodePool
metadata:
  name: default
spec:
  template:
    spec:
      nodeClassRef:
        group: karpenter.k8s.aws
        kind: EC2NodeClass
        name: default
      requirements:
        - key: kubernetes.io/arch
          operator: In
          values: ["amd64"]
        - key: karpenter.sh/capacity-type
          operator: In
          values: ["spot", "on-demand"]   # spot-first
        - key: karpenter.k8s.aws/instance-category
          operator: In
          values: ["c", "m", "r"]
        - key: karpenter.k8s.aws/instance-generation
          operator: Gt
          values: ["2"]
        - key: karpenter.k8s.aws/instance-size
          operator: NotIn
          values: ["nano", "micro", "small"]
  limits:
    cpu: "1000"
    memory: 2000Gi
  disruption:
    consolidationPolicy: WhenEmptyOrUnderutilized
    consolidateAfter: 1m
```

---

## Customising via CDK context

The following `KarpenterConstruct` parameters can be overridden when the construct is instantiated in `python_cdk_stack.py` (or adapted to be driven by CDK context):

| Parameter | Default | Description |
|---|---|---|
| `capacity_types` | `["spot", "on-demand"]` | Allowed capacity types in the default NodePool |
| `instance_categories` | `["c", "m", "r"]` | Instance category filter |
| `instance_generations_gt` | `2` | Only consider generations newer than this value |
| `node_pool_cpu_limit` | `1000` | Total vCPU cap for all Karpenter-managed nodes |
| `node_pool_memory_limit_gi` | `2000` | Total memory cap (GiB) |

---

## Creating additional NodePools

Deploy additional pools directly with `kubectl` after the stack is live. No CDK changes are required.

### Example — GPU node pool

```yaml
apiVersion: karpenter.k8s.aws/v1
kind: EC2NodeClass
metadata:
  name: gpu
spec:
  amiSelectorTerms:
    - alias: al2023@latest
  role: KarpenterNodeRole-<cluster>
  subnetSelectorTerms:
    - tags:
        karpenter.sh/discovery: <cluster>
  securityGroupSelectorTerms:
    - tags:
        karpenter.sh/discovery: <cluster>
  blockDeviceMappings:
    - deviceName: /dev/xvda
      ebs:
        volumeSize: 200Gi
        volumeType: gp3
        encrypted: true
---
apiVersion: karpenter.sh/v1
kind: NodePool
metadata:
  name: gpu
spec:
  template:
    spec:
      nodeClassRef:
        group: karpenter.k8s.aws
        kind: EC2NodeClass
        name: gpu
      requirements:
        - key: karpenter.k8s.aws/instance-category
          operator: In
          values: ["g", "p"]
        - key: karpenter.sh/capacity-type
          operator: In
          values: ["spot", "on-demand"]
      taints:
        - key: nvidia.com/gpu
          value: "true"
          effect: NoSchedule
  limits:
    cpu: "200"
  disruption:
    consolidationPolicy: WhenEmpty
    consolidateAfter: 5m
```

Apply with:

```bash
kubectl apply -f gpu-nodepool.yaml
```

Use a matching toleration and resource request in your workload pods:

```yaml
tolerations:
  - key: nvidia.com/gpu
    operator: Exists
    effect: NoSchedule
resources:
  limits:
    nvidia.com/gpu: "1"
```

### Example — Spot-only arm64 pool

```yaml
apiVersion: karpenter.sh/v1
kind: NodePool
metadata:
  name: arm64-spot
spec:
  template:
    spec:
      nodeClassRef:
        group: karpenter.k8s.aws
        kind: EC2NodeClass
        name: default
      requirements:
        - key: kubernetes.io/arch
          operator: In
          values: ["arm64"]
        - key: karpenter.sh/capacity-type
          operator: In
          values: ["spot"]
        - key: karpenter.k8s.aws/instance-category
          operator: In
          values: ["m", "c", "r"]
  limits:
    cpu: "500"
  disruption:
    consolidationPolicy: WhenEmptyOrUnderutilized
    consolidateAfter: 2m
```

---

## Disruption policies

Karpenter can reclaim nodes in two ways:

| Policy | Behaviour |
|---|---|
| `WhenEmpty` | Consolidates only fully empty nodes (safest for stateful workloads) |
| `WhenEmptyOrUnderutilized` | Bin-packs under-utilised nodes, potentially moving pods (default) |

`consolidateAfter` adds a stabilisation delay before consolidating, reducing unnecessary churn.

---

## Scheduling workloads onto Karpenter nodes

### Prefer Spot, fall back to On-Demand

```yaml
spec:
  nodeSelector:
    karpenter.sh/capacity-type: spot
  tolerations: []   # no special tolerations required for the default pool
```

### Force On-Demand only

```yaml
spec:
  nodeSelector:
    karpenter.sh/capacity-type: on-demand
```

### Target a specific NodePool

```yaml
spec:
  nodeSelector:
    karpenter.sh/nodepool: my-pool-name
```

---

## Observability

```bash
# View Karpenter controller logs
kubectl logs -n karpenter -l app.kubernetes.io/name=karpenter --tail=100 -f

# Check node claims managed by Karpenter
kubectl get nodeclaims

# Check node pool utilisation
kubectl get nodepool default -o jsonpath='{.status.resources}' | jq
```

---

## Node image (AMI) updates

The `EC2NodeClass` uses `alias: al2023@latest`. Karpenter will **not** automatically roll nodes to a newer AMI; it only uses the latest AMI when launching **new** nodes. To force existing nodes onto a new AMI:

```bash
# Trigger a rolling replacement of all Karpenter-managed nodes
kubectl annotate nodeclaim --all \
  karpenter.sh/do-not-disrupt-  2>/dev/null || true

kubectl delete nodeclaim --all
```

Karpenter will re-provision each node with the latest AMI as workloads are rescheduled.
