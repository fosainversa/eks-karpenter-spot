from __future__ import annotations

from constructs import Construct
from aws_cdk import Stack

from python_cdk.constructs.vpc_construct import VpcConstruct
from python_cdk.constructs.eks_construct import EksConstruct
from python_cdk.constructs.karpenter_construct import KarpenterConstruct
from python_cdk.constructs.addons_construct import AddonsConstruct


class PythonCdkStack(Stack):
    """
    Main stack that wires together:

    * VPC  – new or looked-up via ``vpc_id`` context key
    * EKS cluster  – managed node groups (on-demand system + spot general)
    * Karpenter  – dynamic, cost-efficient node provisioning
    * Cluster addons  – ALB Controller, EBS CSI Driver, optional ExternalDNS

    Context keys (set in ``cdk.json`` or via ``--context`` flag)
    -----------------------------------------------------------
    ``cluster_name``          string   required  EKS cluster name
    ``vpc_id``                string   optional  reuse existing VPC; omit to create new
    ``vpc_cidr``              string   optional  CIDR for the new VPC (default 10.0.0.0/16)
    ``kubernetes_version``    string   optional  K8s version (default 1.32)
    ``system_instance_types`` list     optional  on-demand instance types for system nodes
    ``spot_instance_types``   list     optional  spot instance types for general nodes
    ``karpenter_version``     string   optional  Karpenter Helm chart version
    ``install_external_dns``  bool     optional  install ExternalDNS addon (default false)
    ``hosted_zone_arns``      list     optional  Route53 zone ARNs for ExternalDNS
    """

    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # ── Read configuration from CDK context ──────────────────────────────
        cluster_name: str = self.node.try_get_context("cluster_name") or "eks-cluster"
        vpc_id: str | None = self.node.try_get_context("vpc_id")
        vpc_cidr: str = self.node.try_get_context("vpc_cidr") or "10.0.0.0/16"
        kubernetes_version: str = self.node.try_get_context("kubernetes_version") or "1.32"
        system_instance_types: list[str] | None = self.node.try_get_context("system_instance_types")
        spot_instance_types: list[str] | None = self.node.try_get_context("spot_instance_types")
        karpenter_version: str = self.node.try_get_context("karpenter_version") or "1.0.6"
        install_external_dns: bool = bool(
            self.node.try_get_context("install_external_dns")
        )
        hosted_zone_arns: list[str] | None = self.node.try_get_context("hosted_zone_arns")

        # ── VPC ───────────────────────────────────────────────────────────────
        vpc_construct = VpcConstruct(
            self,
            "Vpc",
            cluster_name=cluster_name,
            vpc_id=vpc_id,
            cidr=vpc_cidr,
        )

        # ── EKS Cluster + managed node groups ────────────────────────────────
        eks_construct = EksConstruct(
            self,
            "Eks",
            cluster_name=cluster_name,
            vpc=vpc_construct.vpc,
            kubernetes_version=kubernetes_version,
            system_instance_types=system_instance_types,
            spot_instance_types=spot_instance_types,
        )

        # ── Karpenter ─────────────────────────────────────────────────────────
        KarpenterConstruct(
            self,
            "Karpenter",
            cluster=eks_construct.cluster,
            cluster_name=cluster_name,
            vpc=vpc_construct.vpc,
            karpenter_version=karpenter_version,
        )

        # ── Cluster addons ────────────────────────────────────────────────────
        AddonsConstruct(
            self,
            "Addons",
            cluster=eks_construct.cluster,
            cluster_name=cluster_name,
            vpc=vpc_construct.vpc,
            install_ebs_csi_driver=True,
            install_external_dns=install_external_dns,
            hosted_zone_arns=hosted_zone_arns,
        )

