from __future__ import annotations

from constructs import Construct
from aws_cdk import (
    CfnJson,
    Stack,
    aws_ec2 as ec2,
    aws_eks as eks,
    aws_iam as iam,
)


# Helm chart version for the AWS Load Balancer Controller
ALB_CHART_VERSION = "1.8.1"


class AddonsConstruct(Construct):
    """
    Installs cluster-level addons required to expose applications:

    * **AWS Load Balancer Controller** – provisions ALBs/NLBs for Ingress
      and Service resources.  Uses IRSA for IAM authentication.
    * **AWS EBS CSI Driver** – managed EKS addon, enables PersistentVolume
      support backed by EBS gp3 volumes.
    * **ExternalDNS** (optional) – manages Route 53 records for services.
    """

    def __init__(
        self,
        scope: Construct,
        id: str,
        *,
        cluster: eks.Cluster,
        cluster_name: str,
        vpc: ec2.IVpc,
        install_ebs_csi_driver: bool = True,
        install_external_dns: bool = False,
        hosted_zone_arns: list[str] | None = None,
        alb_chart_version: str = ALB_CHART_VERSION,
    ) -> None:
        super().__init__(scope, id)

        account = Stack.of(self).account
        region = Stack.of(self).region
        oidc_issuer = cluster.cluster_open_id_connect_issuer
        oidc_arn = cluster.open_id_connect_provider.open_id_connect_provider_arn

        # ── AWS Load Balancer Controller ──────────────────────────────────────
        alb_sa_name = "aws-load-balancer-controller"
        alb_namespace = "kube-system"

        alb_irsa_conditions = CfnJson(
            self,
            "AlbIrsaConditions",
            value={
                f"{oidc_issuer}:aud": "sts.amazonaws.com",
                f"{oidc_issuer}:sub": f"system:serviceaccount:{alb_namespace}:{alb_sa_name}",
            },
        )

        alb_role = iam.Role(
            self,
            "AlbControllerRole",
            role_name=f"AWSLoadBalancerControllerRole-{cluster_name}",
            assumed_by=iam.FederatedPrincipal(
                oidc_arn,
                conditions={"StringEquals": alb_irsa_conditions},
                assume_role_action="sts:AssumeRoleWithWebIdentity",
            ),
        )

        self._attach_alb_policy(alb_role, account, region, cluster_name)

        cluster.add_helm_chart(
            "AwsLoadBalancerController",
            chart="aws-load-balancer-controller",
            repository="https://aws.github.io/eks-charts",
            namespace=alb_namespace,
            version=alb_chart_version,
            wait=True,
            values={
                "clusterName": cluster_name,
                "serviceAccount": {
                    "create": True,
                    "name": alb_sa_name,
                    "annotations": {
                        "eks.amazonaws.com/role-arn": alb_role.role_arn,
                    },
                },
                "region": region,
                "vpcId": vpc.vpc_id,
                "tolerations": [
                    {"key": "CriticalAddonsOnly", "operator": "Exists"}
                ],
                "nodeSelector": {"role": "system"},
                "replicaCount": 2,
            },
        )

        # ── AWS EBS CSI Driver (managed addon) ───────────────────────────────
        if install_ebs_csi_driver:
            ebs_irsa_conditions = CfnJson(
                self,
                "EbsIrsaConditions",
                value={
                    f"{oidc_issuer}:aud": "sts.amazonaws.com",
                    f"{oidc_issuer}:sub": (
                        "system:serviceaccount:kube-system:ebs-csi-controller-sa"
                    ),
                },
            )

            ebs_role = iam.Role(
                self,
                "EbsCsiDriverRole",
                role_name=f"AmazonEKS_EBS_CSI_DriverRole-{cluster_name}",
                assumed_by=iam.FederatedPrincipal(
                    oidc_arn,
                    conditions={"StringEquals": ebs_irsa_conditions},
                    assume_role_action="sts:AssumeRoleWithWebIdentity",
                ),
                managed_policies=[
                    iam.ManagedPolicy.from_aws_managed_policy_name(
                        "service-role/AmazonEBSCSIDriverPolicy"
                    )
                ],
            )

            eks.CfnAddon(
                self,
                "EbsCsiAddon",
                cluster_name=cluster_name,
                addon_name="aws-ebs-csi-driver",
                service_account_role_arn=ebs_role.role_arn,
                resolve_conflicts="OVERWRITE",
            )

        # ── ExternalDNS (optional) ────────────────────────────────────────────
        if install_external_dns:
            _hosted_zone_arns = hosted_zone_arns or [f"arn:aws:route53:::hostedzone/*"]

            external_dns_irsa_conditions = CfnJson(
                self,
                "ExternalDnsIrsaConditions",
                value={
                    f"{oidc_issuer}:aud": "sts.amazonaws.com",
                    f"{oidc_issuer}:sub": (
                        "system:serviceaccount:external-dns:external-dns"
                    ),
                },
            )

            external_dns_role = iam.Role(
                self,
                "ExternalDnsRole",
                role_name=f"ExternalDNSRole-{cluster_name}",
                assumed_by=iam.FederatedPrincipal(
                    oidc_arn,
                    conditions={"StringEquals": external_dns_irsa_conditions},
                    assume_role_action="sts:AssumeRoleWithWebIdentity",
                ),
            )

            external_dns_role.add_to_policy(
                iam.PolicyStatement(
                    effect=iam.Effect.ALLOW,
                    actions=["route53:ChangeResourceRecordSets"],
                    resources=_hosted_zone_arns,
                )
            )
            external_dns_role.add_to_policy(
                iam.PolicyStatement(
                    effect=iam.Effect.ALLOW,
                    actions=[
                        "route53:ListHostedZones",
                        "route53:ListResourceRecordSets",
                        "route53:ListTagsForResource",
                    ],
                    resources=["*"],
                )
            )

            cluster.add_helm_chart(
                "ExternalDns",
                chart="external-dns",
                repository="https://kubernetes-sigs.github.io/external-dns/",
                namespace="external-dns",
                create_namespace=True,
                version="1.14.5",
                wait=True,
                values={
                    "provider": {"name": "aws"},
                    "aws": {"region": region},
                    "serviceAccount": {
                        "create": True,
                        "name": "external-dns",
                        "annotations": {
                            "eks.amazonaws.com/role-arn": external_dns_role.role_arn,
                        },
                    },
                    "tolerations": [
                        {"key": "CriticalAddonsOnly", "operator": "Exists"}
                    ],
                    "nodeSelector": {"role": "system"},
                },
            )

    # ── Private helpers ───────────────────────────────────────────────────────

    def _attach_alb_policy(
        self,
        role: iam.Role,
        account: str,
        region: str,
        cluster_name: str,
    ) -> None:
        """Attach the IAM statements required by the AWS Load Balancer Controller
        (https://kubernetes-sigs.github.io/aws-load-balancer-controller/)."""

        role.add_to_policy(iam.PolicyStatement(
            sid="AllowIAMServiceLinkedRole",
            effect=iam.Effect.ALLOW,
            actions=["iam:CreateServiceLinkedRole"],
            resources=["*"],
            conditions={
                "StringEquals": {
                    "iam:AWSServiceName": "elasticloadbalancing.amazonaws.com"
                }
            },
        ))

        role.add_to_policy(iam.PolicyStatement(
            sid="AllowEC2Describe",
            effect=iam.Effect.ALLOW,
            actions=[
                "ec2:DescribeAccountAttributes",
                "ec2:DescribeAddresses",
                "ec2:DescribeAvailabilityZones",
                "ec2:DescribeInternetGateways",
                "ec2:DescribeVpcs",
                "ec2:DescribeVpcPeeringConnections",
                "ec2:DescribeSubnets",
                "ec2:DescribeSecurityGroups",
                "ec2:DescribeInstances",
                "ec2:DescribeNetworkInterfaces",
                "ec2:DescribeTags",
                "ec2:GetCoipPoolUsage",
                "ec2:DescribeCoipPools",
            ],
            resources=["*"],
        ))

        role.add_to_policy(iam.PolicyStatement(
            sid="AllowELBManagement",
            effect=iam.Effect.ALLOW,
            actions=[
                "elasticloadbalancing:DescribeLoadBalancers",
                "elasticloadbalancing:DescribeLoadBalancerAttributes",
                "elasticloadbalancing:DescribeListeners",
                "elasticloadbalancing:DescribeListenerCertificates",
                "elasticloadbalancing:DescribeSSLPolicies",
                "elasticloadbalancing:DescribeRules",
                "elasticloadbalancing:DescribeTargetGroups",
                "elasticloadbalancing:DescribeTargetGroupAttributes",
                "elasticloadbalancing:DescribeTargetHealth",
                "elasticloadbalancing:DescribeTags",
                "elasticloadbalancing:DescribeTrustStores",
            ],
            resources=["*"],
        ))

        role.add_to_policy(iam.PolicyStatement(
            sid="AllowCertificateManagerRead",
            effect=iam.Effect.ALLOW,
            actions=[
                "cognito-idp:DescribeUserPoolClient",
                "acm:ListCertificates",
                "acm:DescribeCertificate",
                "iam:ListServerCertificates",
                "iam:GetServerCertificate",
                "waf-regional:GetWebACL",
                "waf-regional:GetWebACLForResource",
                "waf-regional:AssociateWebACL",
                "waf-regional:DisassociateWebACL",
                "wafv2:GetWebACL",
                "wafv2:GetWebACLForResource",
                "wafv2:AssociateWebACL",
                "wafv2:DisassociateWebACL",
                "shield:GetSubscriptionState",
                "shield:DescribeProtection",
                "shield:CreateProtection",
                "shield:DeleteProtection",
            ],
            resources=["*"],
        ))

        role.add_to_policy(iam.PolicyStatement(
            sid="AllowEC2SecurityGroupManagement",
            effect=iam.Effect.ALLOW,
            actions=[
                "ec2:AuthorizeSecurityGroupIngress",
                "ec2:RevokeSecurityGroupIngress",
            ],
            resources=["*"],
        ))

        role.add_to_policy(iam.PolicyStatement(
            sid="AllowEC2SecurityGroupCreation",
            effect=iam.Effect.ALLOW,
            actions=["ec2:CreateSecurityGroup"],
            resources=["*"],
        ))

        role.add_to_policy(iam.PolicyStatement(
            sid="AllowEC2SecurityGroupTagging",
            effect=iam.Effect.ALLOW,
            actions=["ec2:CreateTags"],
            resources=[f"arn:aws:ec2:{region}:*:security-group/*"],
            conditions={
                "StringEquals": {"ec2:CreateAction": "CreateSecurityGroup"},
                "Null": {"aws:RequestTag/elbv2.k8s.aws/cluster": "false"},
            },
        ))

        role.add_to_policy(iam.PolicyStatement(
            sid="AllowEC2SecurityGroupMutation",
            effect=iam.Effect.ALLOW,
            actions=[
                "ec2:CreateTags",
                "ec2:DeleteTags",
                "ec2:AuthorizeSecurityGroupIngress",
                "ec2:RevokeSecurityGroupIngress",
                "ec2:DeleteSecurityGroup",
            ],
            resources=[f"arn:aws:ec2:{region}:*:security-group/*"],
            conditions={
                "Null": {"aws:RequestTag/elbv2.k8s.aws/cluster": "true",
                         "aws:ResourceTag/elbv2.k8s.aws/cluster": "false"},
            },
        ))

        role.add_to_policy(iam.PolicyStatement(
            sid="AllowELBProvisioningTagged",
            effect=iam.Effect.ALLOW,
            actions=[
                "elasticloadbalancing:CreateLoadBalancer",
                "elasticloadbalancing:CreateTargetGroup",
            ],
            resources=["*"],
            conditions={
                "Null": {"aws:RequestTag/elbv2.k8s.aws/cluster": "false"},
            },
        ))

        role.add_to_policy(iam.PolicyStatement(
            sid="AllowELBRulesAndListeners",
            effect=iam.Effect.ALLOW,
            actions=[
                "elasticloadbalancing:CreateListener",
                "elasticloadbalancing:DeleteListener",
                "elasticloadbalancing:CreateRule",
                "elasticloadbalancing:DeleteRule",
            ],
            resources=["*"],
        ))

        role.add_to_policy(iam.PolicyStatement(
            sid="AllowELBTagging",
            effect=iam.Effect.ALLOW,
            actions=["elasticloadbalancing:AddTags", "elasticloadbalancing:RemoveTags"],
            resources=[
                f"arn:aws:elasticloadbalancing:{region}:*:targetgroup/*/*",
                f"arn:aws:elasticloadbalancing:{region}:*:loadbalancer/net/*/*",
                f"arn:aws:elasticloadbalancing:{region}:*:loadbalancer/app/*/*",
            ],
            conditions={
                "Null": {
                    "aws:RequestTag/elbv2.k8s.aws/cluster": "true",
                    "aws:ResourceTag/elbv2.k8s.aws/cluster": "false",
                },
            },
        ))

        role.add_to_policy(iam.PolicyStatement(
            sid="AllowELBTaggingForListenerRules",
            effect=iam.Effect.ALLOW,
            actions=["elasticloadbalancing:AddTags", "elasticloadbalancing:RemoveTags"],
            resources=[
                f"arn:aws:elasticloadbalancing:{region}:*:listener/net/*/*/*",
                f"arn:aws:elasticloadbalancing:{region}:*:listener/app/*/*/*",
                f"arn:aws:elasticloadbalancing:{region}:*:listener-rule/net/*/*/*",
                f"arn:aws:elasticloadbalancing:{region}:*:listener-rule/app/*/*/*",
            ],
        ))

        role.add_to_policy(iam.PolicyStatement(
            sid="AllowELBModification",
            effect=iam.Effect.ALLOW,
            actions=[
                "elasticloadbalancing:ModifyLoadBalancerAttributes",
                "elasticloadbalancing:SetIpAddressType",
                "elasticloadbalancing:SetSecurityGroups",
                "elasticloadbalancing:SetSubnets",
                "elasticloadbalancing:DeleteLoadBalancer",
                "elasticloadbalancing:ModifyTargetGroup",
                "elasticloadbalancing:ModifyTargetGroupAttributes",
                "elasticloadbalancing:DeleteTargetGroup",
                "elasticloadbalancing:ModifyListenerAttributes",
            ],
            resources=["*"],
            conditions={
                "Null": {"aws:ResourceTag/elbv2.k8s.aws/cluster": "false"},
            },
        ))

        role.add_to_policy(iam.PolicyStatement(
            sid="AllowELBTargetRegistration",
            effect=iam.Effect.ALLOW,
            actions=[
                "elasticloadbalancing:RegisterTargets",
                "elasticloadbalancing:DeregisterTargets",
            ],
            resources=[
                f"arn:aws:elasticloadbalancing:{region}:*:targetgroup/*/*"
            ],
        ))

        role.add_to_policy(iam.PolicyStatement(
            sid="AllowELBListenerModification",
            effect=iam.Effect.ALLOW,
            actions=[
                "elasticloadbalancing:SetWebAcl",
                "elasticloadbalancing:ModifyListener",
                "elasticloadbalancing:AddListenerCertificates",
                "elasticloadbalancing:RemoveListenerCertificates",
                "elasticloadbalancing:ModifyRule",
            ],
            resources=["*"],
        ))
