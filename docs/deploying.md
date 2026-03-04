# Deployment guide

Step-by-step instructions for deploying the EKS platform stack from scratch or into an existing VPC.

---

## Prerequisites

### Tools

| Tool | Minimum version | Install |
|---|---|---|
| Python | 3.9 | [python.org](https://www.python.org/downloads/) |
| Node.js | 20 or 22 LTS | [nodejs.org](https://nodejs.org/) |
| AWS CDK CLI | 2.x | `npm install -g aws-cdk` |
| AWS CLI | 2.x | [docs.aws.amazon.com/cli](https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html) |
| kubectl | ≥ 1.27 | [kubernetes.io/docs](https://kubernetes.io/docs/tasks/tools/) |
| helm | 3.x | [helm.sh/docs](https://helm.sh/docs/intro/install/) |

### AWS credentials

The deploying identity (IAM user or role) needs at minimum:
- `iam:*` on roles/policies created by the stack (or `AdministratorAccess` for initial setup)
- `ec2:*`, `eks:*`, `sqs:*`, `events:*`
- `cloudformation:*`
- `s3:*` and `ssm:GetParameter` (for CDK bootstrap assets)

---

## Step 1 — Clone and set up the Python environment

```bash
git clone <repo-url>
cd python-cdk

python3 -m venv .venv
source .venv/bin/activate         # macOS / Linux
# .venv\Scripts\activate.bat      # Windows

pip install -r requirements.txt
pip install -r requirements-dev.txt   # optional, for tests
```

---

## Step 2 — Configure AWS credentials

```bash
# Option A — named profile
export AWS_PROFILE=my-profile

# Option B — environment variables
export AWS_ACCESS_KEY_ID=...
export AWS_SECRET_ACCESS_KEY=...
export AWS_DEFAULT_REGION=eu-west-1

# Verify
aws sts get-caller-identity
```

---

## Step 3 — CDK bootstrap (first time per account/region)

CDK needs an S3 bucket and ECR repository to store deployment assets.

```bash
cdk bootstrap aws://<ACCOUNT_ID>/<REGION>

# Example
cdk bootstrap aws://123456789012/eu-west-1
```

This only needs to be run once per account/region combination.

---

## Step 4 — Configure your deployment

Edit the `"context"` block in `cdk.json` with your values, or pass them as `--context` flags at deploy time. CLI flags take precedence.

```jsonc
// cdk.json (relevant section)
"context": {
  "cluster_name": "my-eks",
  "vpc_cidr": "10.10.0.0/16",           // omit to use 10.0.0.0/16
  // "vpc_id": "vpc-0abc123456",         // uncomment to reuse existing VPC
  "kubernetes_version": "1.32",
  "karpenter_version": "1.0.6",
  "install_external_dns": false
}
```

### Using an existing VPC

If you already have a VPC, add `vpc_id` to the context. Before deploying, ensure the subnets carry the correct tags:

```bash
# Tag private subnets
aws ec2 create-tags \
  --resources subnet-aaa subnet-bbb subnet-ccc \
  --tags \
    Key=kubernetes.io/role/internal-elb,Value=1 \
    Key=kubernetes.io/cluster/my-eks,Value=owned \
    Key=karpenter.sh/discovery,Value=my-eks

# Tag public subnets
aws ec2 create-tags \
  --resources subnet-xxx subnet-yyy subnet-zzz \
  --tags \
    Key=kubernetes.io/role/elb,Value=1 \
    Key=kubernetes.io/cluster/my-eks,Value=owned \
    Key=karpenter.sh/discovery,Value=my-eks
```

---

## Step 5 — Synthesise (dry-run)

Generate the CloudFormation templates without deploying:

```bash
cdk synth
```

Review the output in `cdk.out/` to confirm the resources match your expectations.

---

## Step 6 — Deploy

```bash
# Minimal — new VPC, defaults
cdk deploy

# Reuse existing VPC
cdk deploy --context vpc_id=vpc-0abc123456

# Full example with overrides
cdk deploy \
  --context cluster_name=my-eks \
  --context vpc_id=vpc-0abc123456 \
  --context kubernetes_version=1.32 \
  --context karpenter_version=1.0.6 \
  --context install_external_dns=true \
  --context hosted_zone_arns='["arn:aws:route53:::hostedzone/Z1XXXXX","arn:aws:route53:::hostedzone/Z2YYYYY"]'
```

CDK will ask for confirmation before creating IAM resources. Pass `--require-approval never` to skip the prompt in CI.

Typical deploy time: **20–35 minutes** (EKS control plane ~10 min, Helm charts ~5 min each).

---

## Step 7 — Configure kubectl

Once the stack is deployed, retrieve the kubeconfig:

```bash
aws eks update-kubeconfig \
  --region <REGION> \
  --name <cluster_name>

# Verify connectivity
kubectl get nodes
```

Expected output — system and spot managed nodes should be `Ready`:

```
NAME                              STATUS   ROLES    AGE   VERSION
ip-10-0-1-xxx.eu-west-1.compute.internal   Ready    <none>   5m    v1.32.x
ip-10-0-2-yyy.eu-west-1.compute.internal   Ready    <none>   5m    v1.32.x
ip-10-0-3-zzz.eu-west-1.compute.internal   Ready    <none>   4m    v1.32.x
ip-10-0-1-aaa.eu-west-1.compute.internal   Ready    <none>   4m    v1.32.x
```

---

## Step 8 — Verify addons

```bash
# Karpenter controller running on a system node
kubectl get pods -n karpenter

# AWS Load Balancer Controller running on system nodes
kubectl get pods -n kube-system -l app.kubernetes.io/name=aws-load-balancer-controller

# EBS CSI Driver daemonset and controller
kubectl get pods -n kube-system -l app=ebs-csi-controller
kubectl get pods -n kube-system -l app=ebs-csi-node

# ExternalDNS (if enabled)
kubectl get pods -n external-dns
```

---

## Step 9 — Verify Karpenter

Check that the default `NodePool` and `EC2NodeClass` exist:

```bash
kubectl get nodepool
kubectl get ec2nodeclass
```

Deploy a test workload to trigger Karpenter scale-out:

```bash
kubectl apply -f - <<EOF
apiVersion: apps/v1
kind: Deployment
metadata:
  name: inflate
spec:
  replicas: 5
  selector:
    matchLabels:
      app: inflate
  template:
    metadata:
      labels:
        app: inflate
    spec:
      terminationGracePeriodSeconds: 0
      containers:
      - name: inflate
        image: public.ecr.aws/eks-distro/kubernetes/pause:3.7
        resources:
          requests:
            cpu: "1"
EOF

kubectl get nodes --watch   # watch new nodes appear
kubectl delete deployment inflate
kubectl get nodes --watch   # watch Karpenter consolidate
```

---

## Upgrading Kubernetes version

1. Update `kubernetes_version` in `cdk.json` and the matching `KubectlVxxLayer` package in `requirements.txt` / `eks_construct.py`.
2. Run `cdk deploy`.
3. CDK updates the EKS control plane. AWS performs a rolling upgrade of the managed node groups automatically.
4. Update the Karpenter `EC2NodeClass.spec.amiSelectorTerms` alias if needed.

---

## Tearing down

```bash
cdk destroy
```

> **Note:** EKS persistent volumes (EBS) and any Route 53 records created by ExternalDNS are **not** managed by CloudFormation and must be cleaned up manually if no longer needed.

---

## Troubleshooting

| Symptom | Likely cause | Resolution |
|---|---|---|
| `cdk synth` fails with OIDC token error | CDK token used as map key | `CfnJson` wrapping already handles this; check for CDK lib version mismatch |
| Nodes not joining cluster | Node IAM role not in `aws-auth` ConfigMap | The construct calls `cluster.aws_auth.add_role_mapping` automatically; run `kubectl describe configmap aws-auth -n kube-system` to verify |
| ALB not provisioned | Subnets missing ELB tags | Apply the tags described in Step 4 |
| Karpenter not launching nodes | `EC2NodeClass` misconfigured | Run `kubectl describe nodepool default` and `kubectl describe ec2nodeclass default` |
| `kubectl` times out | VPN / network required for private endpoint | Either enable the public endpoint or use a bastion / VPN |
