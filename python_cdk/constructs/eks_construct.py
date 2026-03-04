from __future__ import annotations

from constructs import Construct
from aws_cdk import (
    Tags,
    aws_ec2 as ec2,
    aws_eks as eks,
    aws_iam as iam,
)
from aws_cdk.lambda_layer_kubectl_v32 import KubectlV32Layer


class EksConstruct(Construct):
    """
    Creates an EKS cluster with two managed node groups:

    * **system**  – On-Demand instances for kube-system and Karpenter
      controller pods. These nodes ensure cluster operations are not
      disrupted by Spot interruptions.
    * **spot**    – Spot instances as a second managed node group for
      general, non-critical workloads running alongside Karpenter-
      provisioned capacity.

    The construct also exposes helpers consumed by downstream constructs
    (Karpenter, ALB Controller, etc.).
    """

    def __init__(
        self,
        scope: Construct,
        id: str,
        *,
        cluster_name: str,
        vpc: ec2.IVpc,
        kubernetes_version: str = "1.32",
        system_instance_types: list[str] | None = None,
        spot_instance_types: list[str] | None = None,
        system_min_size: int = 2,
        system_max_size: int = 4,
        system_desired_size: int = 2,
        spot_min_size: int = 1,
        spot_max_size: int = 10,
        spot_desired_size: int = 2,
    ) -> None:
        super().__init__(scope, id)

        _system_instance_types = system_instance_types or ["m5.large", "m5a.large", "m6i.large"]
        _spot_instance_types = spot_instance_types or [
            "m5.large", "m5a.large", "m5d.large",
            "m6i.large", "m6a.large",
            "c5.xlarge", "c5a.xlarge", "c6i.xlarge",
            "r5.large", "r5a.large",
        ]

        k8s_version = eks.KubernetesVersion.of(kubernetes_version)

        # ── IAM role for the EKS control plane ──────────────────────────────
        cluster_role = iam.Role(
            self,
            "ClusterRole",
            assumed_by=iam.ServicePrincipal("eks.amazonaws.com"),
            managed_policies=[
                iam.ManagedPolicy.from_aws_managed_policy_name("AmazonEKSClusterPolicy"),
            ],
        )

        # ── Node IAM role shared by both managed node groups ─────────────────
        self.node_role = iam.Role(
            self,
            "NodeRole",
            role_name=f"EksNodeRole-{cluster_name}",
            assumed_by=iam.ServicePrincipal("ec2.amazonaws.com"),
            managed_policies=[
                iam.ManagedPolicy.from_aws_managed_policy_name("AmazonEKSWorkerNodePolicy"),
                iam.ManagedPolicy.from_aws_managed_policy_name("AmazonEC2ContainerRegistryReadOnly"),
                iam.ManagedPolicy.from_aws_managed_policy_name("AmazonEKS_CNI_Policy"),
                # Required for Karpenter node discovery & SSM access
                iam.ManagedPolicy.from_aws_managed_policy_name("AmazonSSMManagedInstanceCore"),
            ],
        )

        # ── EKS Cluster ───────────────────────────────────────────────────────
        self.cluster = eks.Cluster(
            self,
            "Cluster",
            cluster_name=cluster_name,
            version=k8s_version,
            role=cluster_role,
            vpc=vpc,
            # Place control-plane ENIs in private subnets only
            vpc_subnets=[ec2.SubnetSelection(subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS)],
            # Public + private endpoint so kubectl works locally and in-cluster
            endpoint_access=eks.EndpointAccess.PUBLIC_AND_PRIVATE,
            # Disable the CDK-managed default node group; we add our own below
            default_capacity=0,
            # kubectl Lambda layer matching the cluster Kubernetes version
            kubectl_layer=KubectlV32Layer(self, "KubectlLayer"),
        )

        # Tag the cluster primary security group for Karpenter discovery
        Tags.of(self.cluster.cluster_security_group).add(
            "karpenter.sh/discovery", cluster_name
        )

        # ── System node group (On-Demand) ─────────────────────────────────────
        self.system_nodegroup = self.cluster.add_nodegroup_capacity(
            "SystemNodes",
            nodegroup_name=f"{cluster_name}-system",
            instance_types=[ec2.InstanceType(t) for t in _system_instance_types],
            capacity_type=eks.CapacityType.ON_DEMAND,
            ami_type=eks.NodegroupAmiType.AL2023_X86_64_STANDARD,
            node_role=self.node_role,
            min_size=system_min_size,
            max_size=system_max_size,
            desired_size=system_desired_size,
            subnets=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS),
            # Taint so only kube-system / Karpenter tolerate these nodes
            taints=[
                eks.TaintSpec(
                    key="CriticalAddonsOnly",
                    value="true",
                    effect=eks.TaintEffect.NO_SCHEDULE,
                )
            ],
            labels={"role": "system"},
            tags={
                "Name": f"{cluster_name}-system-node",
                "eks:nodegroup-type": "system",
            },
        )

        # ── Spot node group (Spot) – general workloads alongside Karpenter ───
        self.spot_nodegroup = self.cluster.add_nodegroup_capacity(
            "SpotNodes",
            nodegroup_name=f"{cluster_name}-spot",
            instance_types=[ec2.InstanceType(t) for t in _spot_instance_types],
            capacity_type=eks.CapacityType.SPOT,
            ami_type=eks.NodegroupAmiType.AL2023_X86_64_STANDARD,
            node_role=self.node_role,
            min_size=spot_min_size,
            max_size=spot_max_size,
            desired_size=spot_desired_size,
            subnets=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS),
            labels={"role": "spot-worker"},
            tags={
                "Name": f"{cluster_name}-spot-node",
                "eks:nodegroup-type": "spot",
            },
        )

    # ── Convenience properties ────────────────────────────────────────────────

    @property
    def oidc_provider(self) -> eks.OpenIdConnectProvider:
        return self.cluster.open_id_connect_provider  # type: ignore[return-value]

    @property
    def cluster_name(self) -> str:
        return self.cluster.cluster_name
