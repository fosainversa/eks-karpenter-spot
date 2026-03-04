from __future__ import annotations

from constructs import Construct
from aws_cdk import (
    Tags,
    aws_ec2 as ec2,
)


class VpcConstruct(Construct):
    """
    Provides a VPC for the EKS cluster.

    If ``vpc_id`` is supplied the construct performs a lookup and reuses the
    existing VPC (subnets must already carry the required tags – see notes
    below).  Otherwise a brand-new VPC is created and tagged automatically.

    Required subnet tags when bringing an existing VPC
    ---------------------------------------------------
    Public subnets  : ``kubernetes.io/role/elb = 1``
    Private subnets : ``kubernetes.io/role/internal-elb = 1``
    Both            : ``karpenter.sh/discovery = <cluster_name>``
                      ``kubernetes.io/cluster/<cluster_name> = owned`` (or shared)
    """

    def __init__(
        self,
        scope: Construct,
        id: str,
        *,
        cluster_name: str,
        vpc_id: str | None = None,
        cidr: str = "10.0.0.0/16",
        max_azs: int = 3,
        nat_gateways: int = 1,
    ) -> None:
        super().__init__(scope, id)

        self._cluster_name = cluster_name

        if vpc_id:
            # ── Reuse an existing VPC ────────────────────────────────
            self.vpc = ec2.Vpc.from_lookup(self, "ExistingVpc", vpc_id=vpc_id)
        else:
            # ── Create a new VPC ─────────────────────────────────────
            self.vpc = ec2.Vpc(
                self,
                "Vpc",
                ip_addresses=ec2.IpAddresses.cidr(cidr),
                max_azs=max_azs,
                nat_gateways=nat_gateways,
                subnet_configuration=[
                    ec2.SubnetConfiguration(
                        name="Public",
                        subnet_type=ec2.SubnetType.PUBLIC,
                        cidr_mask=24,
                    ),
                    ec2.SubnetConfiguration(
                        name="Private",
                        subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS,
                        cidr_mask=24,
                    ),
                ],
                # Enable DNS support required by EKS
                enable_dns_support=True,
                enable_dns_hostnames=True,
            )

            self._apply_subnet_tags()

    # ── Private helpers ──────────────────────────────────────────────────────

    def _apply_subnet_tags(self) -> None:
        """Tag subnets so that EKS auto-discovers them and Karpenter can
        select the correct ones for node placement."""
        cluster_tag_key = f"kubernetes.io/cluster/{self._cluster_name}"

        for subnet in self.vpc.public_subnets:
            Tags.of(subnet).add("kubernetes.io/role/elb", "1")
            Tags.of(subnet).add(cluster_tag_key, "owned")
            Tags.of(subnet).add("karpenter.sh/discovery", self._cluster_name)

        for subnet in self.vpc.private_subnets:
            Tags.of(subnet).add("kubernetes.io/role/internal-elb", "1")
            Tags.of(subnet).add(cluster_tag_key, "owned")
            Tags.of(subnet).add("karpenter.sh/discovery", self._cluster_name)

    # ── Public properties ────────────────────────────────────────────────────

    @property
    def private_subnets(self) -> list[ec2.ISubnet]:
        return list(self.vpc.private_subnets)

    @property
    def public_subnets(self) -> list[ec2.ISubnet]:
        return list(self.vpc.public_subnets)
