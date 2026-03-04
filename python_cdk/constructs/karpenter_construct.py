from __future__ import annotations

from constructs import Construct
from aws_cdk import (
    CfnJson,
    Duration,
    RemovalPolicy,
    Stack,
    Tags,
    aws_ec2 as ec2,
    aws_eks as eks,
    aws_events as events,
    aws_events_targets as targets,
    aws_iam as iam,
    aws_sqs as sqs,
)


# Karpenter Helm chart version – update to match your desired release.
KARPENTER_VERSION = "1.0.6"


class KarpenterConstruct(Construct):
    """
    Installs Karpenter on an existing EKS cluster.

    Resources created
    -----------------
    * IAM role for Karpenter-provisioned nodes (``KarpenterNodeRole-<name>``)
    * EC2 instance profile wrapping the node role
    * Karpenter controller IAM role (IRSA)
    * SQS interruption queue + four EventBridge rules
    * Karpenter Helm chart (v1.x) in the ``karpenter`` namespace
    * Default ``NodePool`` and ``EC2NodeClass`` Kubernetes manifests
    """

    def __init__(
        self,
        scope: Construct,
        id: str,
        *,
        cluster: eks.Cluster,
        cluster_name: str,
        vpc: ec2.IVpc,
        karpenter_version: str = KARPENTER_VERSION,
        node_pool_name: str = "default",
        # Karpenter NodePool capacity types
        capacity_types: list[str] | None = None,
        # Instance categories / generations used in the default NodePool
        instance_categories: list[str] | None = None,
        instance_generations_gt: int = 2,
        node_pool_cpu_limit: int = 1000,
        node_pool_memory_limit_gi: int = 2000,
    ) -> None:
        super().__init__(scope, id)

        _capacity_types = capacity_types or ["spot", "on-demand"]
        _instance_categories = instance_categories or ["c", "m", "r"]
        region = Stack.of(self).region
        account = Stack.of(self).account

        # ── Karpenter node IAM role ──────────────────────────────────────────
        self.node_role = iam.Role(
            self,
            "KarpenterNodeRole",
            role_name=f"KarpenterNodeRole-{cluster_name}",
            assumed_by=iam.ServicePrincipal("ec2.amazonaws.com"),
            managed_policies=[
                iam.ManagedPolicy.from_aws_managed_policy_name("AmazonEKSWorkerNodePolicy"),
                iam.ManagedPolicy.from_aws_managed_policy_name("AmazonEC2ContainerRegistryReadOnly"),
                iam.ManagedPolicy.from_aws_managed_policy_name("AmazonEKS_CNI_Policy"),
                iam.ManagedPolicy.from_aws_managed_policy_name("AmazonSSMManagedInstanceCore"),
            ],
        )

        # EC2 instance profile consumed by Karpenter when launching nodes
        self.instance_profile = iam.CfnInstanceProfile(
            self,
            "KarpenterInstanceProfile",
            instance_profile_name=f"KarpenterNodeInstanceProfile-{cluster_name}",
            roles=[self.node_role.role_name],
        )

        # ── SQS interruption queue ────────────────────────────────────────────
        self.interruption_queue = sqs.Queue(
            self,
            "InterruptionQueue",
            queue_name=cluster_name,
            # Karpenter processes messages within seconds; 5 min visibility is safe
            visibility_timeout=Duration.seconds(300),
            # Retain so in-flight messages survive CDK updates
            removal_policy=RemovalPolicy.DESTROY,
            encryption=sqs.QueueEncryption.SQS_MANAGED,
        )

        # Allow EventBridge (events.amazonaws.com) to send messages
        self.interruption_queue.add_to_resource_policy(
            iam.PolicyStatement(
                effect=iam.Effect.ALLOW,
                principals=[iam.ServicePrincipal("events.amazonaws.com"),
                            iam.ServicePrincipal("sqs.amazonaws.com")],
                actions=["sqs:SendMessage"],
                resources=[self.interruption_queue.queue_arn],
            )
        )

        queue_target = targets.SqsQueue(self.interruption_queue)

        # Spot interruption warnings
        events.Rule(
            self, "SpotInterruptionRule",
            event_pattern=events.EventPattern(
                source=["aws.ec2"],
                detail_type=["EC2 Spot Instance Interruption Warning"],
            ),
            targets=[queue_target],
        )

        # Rebalance recommendations
        events.Rule(
            self, "RebalanceRule",
            event_pattern=events.EventPattern(
                source=["aws.ec2"],
                detail_type=["EC2 Instance Rebalance Recommendation"],
            ),
            targets=[queue_target],
        )

        # Instance state-change notifications
        events.Rule(
            self, "InstanceStateChangeRule",
            event_pattern=events.EventPattern(
                source=["aws.ec2"],
                detail_type=["EC2 Instance State-change Notification"],
            ),
            targets=[queue_target],
        )

        # AWS Health – scheduled instance events (maintenance)
        events.Rule(
            self, "ScheduledChangeRule",
            event_pattern=events.EventPattern(
                source=["aws.health"],
                detail_type=["AWS Health Event"],
            ),
            targets=[queue_target],
        )

        # ── Karpenter controller IAM role (IRSA) ─────────────────────────────
        oidc_issuer = cluster.cluster_open_id_connect_issuer  # without https://
        oidc_arn = cluster.open_id_connect_provider.open_id_connect_provider_arn

        # CfnJson is required because oidc_issuer is a CloudFormation token and
        # cannot be used directly as a map key — it must resolve at deploy time.
        karpenter_irsa_conditions = CfnJson(
            self,
            "KarpenterIrsaConditions",
            value={
                f"{oidc_issuer}:aud": "sts.amazonaws.com",
                f"{oidc_issuer}:sub": "system:serviceaccount:karpenter:karpenter",
            },
        )

        self.controller_role = iam.Role(
            self,
            "KarpenterControllerRole",
            role_name=f"KarpenterControllerRole-{cluster_name}",
            assumed_by=iam.FederatedPrincipal(
                oidc_arn,
                conditions={"StringEquals": karpenter_irsa_conditions},
                assume_role_action="sts:AssumeRoleWithWebIdentity",
            ),
        )

        self.controller_role.add_to_policy(
            iam.PolicyStatement(
                sid="AllowScopedEC2InstanceAccessActions",
                effect=iam.Effect.ALLOW,
                actions=["ec2:RunInstances", "ec2:CreateFleet"],
                resources=[
                    f"arn:aws:ec2:{region}::image/*",
                    f"arn:aws:ec2:{region}::snapshot/*",
                    f"arn:aws:ec2:{region}:*:security-group/*",
                    f"arn:aws:ec2:{region}:*:subnet/*",
                ],
            )
        )

        self.controller_role.add_to_policy(
            iam.PolicyStatement(
                sid="AllowScopedEC2LaunchTemplateAccessActions",
                effect=iam.Effect.ALLOW,
                actions=["ec2:RunInstances", "ec2:CreateFleet"],
                resources=[f"arn:aws:ec2:{region}:*:launch-template/*"],
                conditions={
                    "StringEquals": {"aws:ResourceTag/kubernetes.io/cluster/" + cluster_name: "owned"},
                    "StringLike": {"aws:ResourceTag/karpenter.sh/nodepool": "*"},
                },
            )
        )

        self.controller_role.add_to_policy(
            iam.PolicyStatement(
                sid="AllowScopedEC2InstanceActionsWithTags",
                effect=iam.Effect.ALLOW,
                actions=[
                    "ec2:RunInstances",
                    "ec2:CreateFleet",
                    "ec2:CreateLaunchTemplate",
                ],
                resources=[
                    f"arn:aws:ec2:{region}:*:fleet/*",
                    f"arn:aws:ec2:{region}:*:instance/*",
                    f"arn:aws:ec2:{region}:*:volume/*",
                    f"arn:aws:ec2:{region}:*:network-interface/*",
                    f"arn:aws:ec2:{region}:*:launch-template/*",
                    f"arn:aws:ec2:{region}:*:spot-instances-request/*",
                ],
                conditions={
                    "StringEquals": {"aws:RequestTag/kubernetes.io/cluster/" + cluster_name: "owned"},
                    "StringLike": {"aws:RequestTag/karpenter.sh/nodepool": "*"},
                },
            )
        )

        self.controller_role.add_to_policy(
            iam.PolicyStatement(
                sid="AllowScopedResourceCreationTagging",
                effect=iam.Effect.ALLOW,
                actions=["ec2:CreateTags"],
                resources=[
                    f"arn:aws:ec2:{region}:*:fleet/*",
                    f"arn:aws:ec2:{region}:*:instance/*",
                    f"arn:aws:ec2:{region}:*:volume/*",
                    f"arn:aws:ec2:{region}:*:network-interface/*",
                    f"arn:aws:ec2:{region}:*:launch-template/*",
                    f"arn:aws:ec2:{region}:*:spot-instances-request/*",
                ],
                conditions={
                    "StringEquals": {"ec2:CreateAction": [
                        "RunInstances", "CreateFleet", "CreateLaunchTemplate"
                    ]},
                },
            )
        )

        self.controller_role.add_to_policy(
            iam.PolicyStatement(
                sid="AllowScopedResourceTagging",
                effect=iam.Effect.ALLOW,
                actions=["ec2:CreateTags"],
                resources=[f"arn:aws:ec2:{region}:*:instance/*"],
                conditions={
                    "StringEquals": {"aws:ResourceTag/kubernetes.io/cluster/" + cluster_name: "owned"},
                    "StringLike": {"aws:ResourceTag/karpenter.sh/nodepool": "*"},
                },
            )
        )

        self.controller_role.add_to_policy(
            iam.PolicyStatement(
                sid="AllowScopedDeletion",
                effect=iam.Effect.ALLOW,
                actions=["ec2:TerminateInstances", "ec2:DeleteLaunchTemplate"],
                resources=[
                    f"arn:aws:ec2:{region}:*:instance/*",
                    f"arn:aws:ec2:{region}:*:launch-template/*",
                ],
                conditions={
                    "StringEquals": {"aws:ResourceTag/kubernetes.io/cluster/" + cluster_name: "owned"},
                    "StringLike": {"aws:ResourceTag/karpenter.sh/nodepool": "*"},
                },
            )
        )

        self.controller_role.add_to_policy(
            iam.PolicyStatement(
                sid="AllowRegionalReadActions",
                effect=iam.Effect.ALLOW,
                actions=[
                    "ec2:DescribeAvailabilityZones",
                    "ec2:DescribeImages",
                    "ec2:DescribeInstances",
                    "ec2:DescribeInstanceTypeOfferings",
                    "ec2:DescribeInstanceTypes",
                    "ec2:DescribeLaunchTemplates",
                    "ec2:DescribeSecurityGroups",
                    "ec2:DescribeSpotPriceHistory",
                    "ec2:DescribeSubnets",
                ],
                resources=["*"],
                conditions={"StringEquals": {"aws:RequestedRegion": region}},
            )
        )

        self.controller_role.add_to_policy(
            iam.PolicyStatement(
                sid="AllowGlobalReadActions",
                effect=iam.Effect.ALLOW,
                actions=[
                    "ec2:DescribeInstanceTopology",
                    "pricing:GetProducts",
                    "ssm:GetParameter",
                    "eks:DescribeCluster",
                ],
                resources=["*"],
            )
        )

        self.controller_role.add_to_policy(
            iam.PolicyStatement(
                sid="AllowInterruptionQueueActions",
                effect=iam.Effect.ALLOW,
                actions=[
                    "sqs:DeleteMessage",
                    "sqs:GetQueueAttributes",
                    "sqs:GetQueueUrl",
                    "sqs:ReceiveMessage",
                ],
                resources=[self.interruption_queue.queue_arn],
            )
        )

        self.controller_role.add_to_policy(
            iam.PolicyStatement(
                sid="AllowPassingInstanceRole",
                effect=iam.Effect.ALLOW,
                actions=["iam:PassRole"],
                resources=[self.node_role.role_arn],
                conditions={"StringEquals": {"iam:PassedToService": "ec2.amazonaws.com"}},
            )
        )

        self.controller_role.add_to_policy(
            iam.PolicyStatement(
                sid="AllowScopedInstanceProfileCreationActions",
                effect=iam.Effect.ALLOW,
                actions=[
                    "iam:CreateInstanceProfile",
                    "iam:TagInstanceProfile",
                    "iam:AddRoleToInstanceProfile",
                    "iam:RemoveRoleFromInstanceProfile",
                    "iam:DeleteInstanceProfile",
                    "iam:GetInstanceProfile",
                ],
                resources=["*"],
                conditions={
                    "StringEquals": {
                        "aws:RequestTag/kubernetes.io/cluster/" + cluster_name: "owned",
                        "aws:RequestTag/topology.kubernetes.io/region": region,
                    },
                    "StringLike": {"aws:RequestTag/karpenter.k8s.aws/ec2nodeclass": "*"},
                },
            )
        )

        self.controller_role.add_to_policy(
            iam.PolicyStatement(
                sid="AllowScopedInstanceProfileTagActions",
                effect=iam.Effect.ALLOW,
                actions=[
                    "iam:TagInstanceProfile",
                    "iam:AddRoleToInstanceProfile",
                    "iam:RemoveRoleFromInstanceProfile",
                    "iam:DeleteInstanceProfile",
                    "iam:GetInstanceProfile",
                ],
                resources=[f"arn:aws:iam::{account}:instance-profile/*"],
                conditions={
                    "StringEquals": {
                        "aws:ResourceTag/kubernetes.io/cluster/" + cluster_name: "owned",
                        "aws:ResourceTag/topology.kubernetes.io/region": region,
                    },
                    "StringLike": {"aws:ResourceTag/karpenter.k8s.aws/ec2nodeclass": "*"},
                },
            )
        )

        # Grant Karpenter access to the EKS cluster (needed for node registration)
        cluster.aws_auth.add_role_mapping(
            self.node_role,
            groups=["system:bootstrappers", "system:nodes"],
            username="system:node:{{EC2PrivateDNSName}}",
        )

        # ── Karpenter Helm chart ──────────────────────────────────────────────
        helm_chart = cluster.add_helm_chart(
            "Karpenter",
            chart="karpenter",
            repository="oci://public.ecr.aws/karpenter",
            namespace="karpenter",
            create_namespace=True,
            version=karpenter_version,
            wait=True,
            values={
                "serviceAccount": {
                    "annotations": {
                        "eks.amazonaws.com/role-arn": self.controller_role.role_arn,
                    }
                },
                "settings": {
                    "clusterName": cluster_name,
                    "interruptionQueue": self.interruption_queue.queue_name,
                },
                "controller": {
                    "resources": {
                        "requests": {"cpu": "1", "memory": "1Gi"},
                        "limits": {"cpu": "1", "memory": "1Gi"},
                    }
                },
                # Schedule Karpenter itself on the system On-Demand node group
                "tolerations": [
                    {"key": "CriticalAddonsOnly", "operator": "Exists"}
                ],
                "nodeSelector": {"role": "system"},
                "affinity": {
                    "nodeAffinity": {
                        "requiredDuringSchedulingIgnoredDuringExecution": {
                            "nodeSelectorTerms": [{
                                "matchExpressions": [{
                                    "key": "role",
                                    "operator": "In",
                                    "values": ["system"],
                                }]
                            }]
                        }
                    }
                },
            },
        )

        # ── Default EC2NodeClass ──────────────────────────────────────────────
        ec2_node_class = cluster.add_manifest(
            "KarpenterEC2NodeClass",
            {
                "apiVersion": "karpenter.k8s.aws/v1",
                "kind": "EC2NodeClass",
                "metadata": {"name": node_pool_name},
                "spec": {
                    "amiSelectorTerms": [{"alias": "al2023@latest"}],
                    # Reference the node IAM role by name (without arn: prefix)
                    "role": self.node_role.role_name,
                    "subnetSelectorTerms": [
                        {"tags": {"karpenter.sh/discovery": cluster_name}}
                    ],
                    "securityGroupSelectorTerms": [
                        {"tags": {"karpenter.sh/discovery": cluster_name}}
                    ],
                    "tags": {
                        "karpenter.sh/discovery": cluster_name,
                        f"kubernetes.io/cluster/{cluster_name}": "owned",
                    },
                    "blockDeviceMappings": [{
                        "deviceName": "/dev/xvda",
                        "ebs": {
                            "volumeSize": "50Gi",
                            "volumeType": "gp3",
                            "encrypted": True,
                        },
                    }],
                },
            },
        )

        # ── Default NodePool ─────────────────────────────────────────────────
        node_pool = cluster.add_manifest(
            "KarpenterNodePool",
            {
                "apiVersion": "karpenter.sh/v1",
                "kind": "NodePool",
                "metadata": {"name": node_pool_name},
                "spec": {
                    "template": {
                        "spec": {
                            "nodeClassRef": {
                                "group": "karpenter.k8s.aws",
                                "kind": "EC2NodeClass",
                                "name": node_pool_name,
                            },
                            "requirements": [
                                {
                                    "key": "kubernetes.io/arch",
                                    "operator": "In",
                                    "values": ["amd64"],
                                },
                                {
                                    "key": "karpenter.sh/capacity-type",
                                    "operator": "In",
                                    "values": _capacity_types,
                                },
                                {
                                    "key": "karpenter.k8s.aws/instance-category",
                                    "operator": "In",
                                    "values": _instance_categories,
                                },
                                {
                                    "key": "karpenter.k8s.aws/instance-generation",
                                    "operator": "Gt",
                                    "values": [str(instance_generations_gt)],
                                },
                                {
                                    "key": "karpenter.k8s.aws/instance-size",
                                    "operator": "NotIn",
                                    "values": ["nano", "micro", "small"],
                                },
                            ],
                        }
                    },
                    "limits": {
                        "cpu": str(node_pool_cpu_limit),
                        "memory": f"{node_pool_memory_limit_gi}Gi",
                    },
                    "disruption": {
                        "consolidationPolicy": "WhenEmptyOrUnderutilized",
                        "consolidateAfter": "1m",
                    },
                },
            },
        )

        # The CRDs are installed by the Helm chart, so manifests must wait for it
        ec2_node_class.node.add_dependency(helm_chart)
        node_pool.node.add_dependency(ec2_node_class)
