"""Unit tests for the PythonCdkStack (EKS + Karpenter + Spot platform).

These tests synthesise the stack and assert that the key AWS resources
and Kubernetes manifests are present in the generated CloudFormation
template.  They run offline — no AWS credentials required.
"""

import aws_cdk as cdk
import aws_cdk.assertions as assertions

from python_cdk.python_cdk_stack import PythonCdkStack


# ── Helpers ──────────────────────────────────────────────────────────────────

def _synth_stack(
    cluster_name: str = "test-cluster",
    **extra_context: object,
) -> assertions.Template:
    """Create and synthesise the stack with the given context values."""
    app = cdk.App(
        context={
            "cluster_name": cluster_name,
            **extra_context,
        }
    )
    stack = PythonCdkStack(app, "TestStack")
    return assertions.Template.from_stack(stack)


# ── VPC ──────────────────────────────────────────────────────────────────────

class TestVpc:
    """Verify a new VPC is created when no vpc_id is provided."""

    def test_vpc_created(self):
        template = _synth_stack()
        template.resource_count_is("AWS::EC2::VPC", 1)

    def test_private_and_public_subnets_created(self):
        template = _synth_stack()
        # AZ count depends on the environment; in tests CDK typically resolves 2 AZs
        # 2 AZs × 2 subnet types (public + private) = 4 subnets
        subnet_count = len(template.find_resources("AWS::EC2::Subnet"))
        assert subnet_count >= 4 and subnet_count % 2 == 0

    def test_nat_gateway_created(self):
        template = _synth_stack()
        template.resource_count_is("AWS::EC2::NatGateway", 1)


# ── EKS Cluster ─────────────────────────────────────────────────────────────

class TestEksCluster:
    """Verify the EKS cluster and its managed node groups."""

    def test_cluster_created(self):
        template = _synth_stack()
        template.has_resource_properties("Custom::AWSCDK-EKS-Cluster", {
            "Config": assertions.Match.object_like({
                "name": "test-cluster",
            }),
        })

    def test_system_nodegroup_is_on_demand(self):
        template = _synth_stack()
        template.has_resource_properties("AWS::EKS::Nodegroup", {
            "NodegroupName": "test-cluster-system",
            "CapacityType": "ON_DEMAND",
        })

    def test_spot_nodegroup_is_spot(self):
        template = _synth_stack()
        template.has_resource_properties("AWS::EKS::Nodegroup", {
            "NodegroupName": "test-cluster-spot",
            "CapacityType": "SPOT",
        })

    def test_two_nodegroups_created(self):
        template = _synth_stack()
        template.resource_count_is("AWS::EKS::Nodegroup", 2)


# ── Karpenter ────────────────────────────────────────────────────────────────

class TestKarpenter:
    """Verify Karpenter supporting infrastructure."""

    def test_interruption_queue_created(self):
        template = _synth_stack()
        template.has_resource_properties("AWS::SQS::Queue", {
            "QueueName": "test-cluster",
        })

    def test_spot_interruption_eventbridge_rule(self):
        template = _synth_stack()
        template.has_resource_properties("AWS::Events::Rule", {
            "EventPattern": assertions.Match.object_like({
                "source": ["aws.ec2"],
                "detail-type": ["EC2 Spot Instance Interruption Warning"],
            }),
        })

    def test_rebalance_eventbridge_rule(self):
        template = _synth_stack()
        template.has_resource_properties("AWS::Events::Rule", {
            "EventPattern": assertions.Match.object_like({
                "source": ["aws.ec2"],
                "detail-type": ["EC2 Instance Rebalance Recommendation"],
            }),
        })

    def test_instance_state_change_eventbridge_rule(self):
        template = _synth_stack()
        template.has_resource_properties("AWS::Events::Rule", {
            "EventPattern": assertions.Match.object_like({
                "source": ["aws.ec2"],
                "detail-type": ["EC2 Instance State-change Notification"],
            }),
        })

    def test_health_eventbridge_rule(self):
        template = _synth_stack()
        template.has_resource_properties("AWS::Events::Rule", {
            "EventPattern": assertions.Match.object_like({
                "source": ["aws.health"],
                "detail-type": ["AWS Health Event"],
            }),
        })

    def test_four_eventbridge_rules_total(self):
        template = _synth_stack()
        template.resource_count_is("AWS::Events::Rule", 4)

    def test_karpenter_node_role_created(self):
        template = _synth_stack()
        template.has_resource_properties("AWS::IAM::Role", {
            "RoleName": "KarpenterNodeRole-test-cluster",
        })

    def test_karpenter_controller_role_created(self):
        template = _synth_stack()
        template.has_resource_properties("AWS::IAM::Role", {
            "RoleName": "KarpenterControllerRole-test-cluster",
        })

    def test_karpenter_instance_profile_created(self):
        template = _synth_stack()
        template.has_resource_properties("AWS::IAM::InstanceProfile", {
            "InstanceProfileName": "KarpenterNodeInstanceProfile-test-cluster",
        })


# ── Addons ───────────────────────────────────────────────────────────────────

class TestAddons:
    """Verify cluster addons."""

    def test_alb_controller_role_created(self):
        template = _synth_stack()
        template.has_resource_properties("AWS::IAM::Role", {
            "RoleName": "AWSLoadBalancerControllerRole-test-cluster",
        })

    def test_ebs_csi_addon_created(self):
        template = _synth_stack()
        template.has_resource_properties("AWS::EKS::Addon", {
            "AddonName": "aws-ebs-csi-driver",
        })

    def test_ebs_csi_role_created(self):
        template = _synth_stack()
        template.has_resource_properties("AWS::IAM::Role", {
            "RoleName": "AmazonEKS_EBS_CSI_DriverRole-test-cluster",
        })


# ── Context-driven behaviour ────────────────────────────────────────────────

class TestContextOptions:
    """Verify that CDK context keys influence resource configuration."""

    def test_custom_cluster_name_propagates(self):
        template = _synth_stack(cluster_name="my-custom-cluster")
        template.has_resource_properties("Custom::AWSCDK-EKS-Cluster", {
            "Config": assertions.Match.object_like({
                "name": "my-custom-cluster",
            }),
        })

    def test_custom_cluster_name_in_nodegroup(self):
        template = _synth_stack(cluster_name="my-custom-cluster")
        template.has_resource_properties("AWS::EKS::Nodegroup", {
            "NodegroupName": "my-custom-cluster-system",
        })
