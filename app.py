#!/usr/bin/env python3
"""Entry point for the Python CDK EKS application.

Deploy
------
# New VPC (all defaults):
cdk deploy

# Existing VPC:
cdk deploy --context vpc_id=vpc-0abc123456

# Full example:
cdk deploy \\
  --context cluster_name=my-eks \\
  --context vpc_id=vpc-0abc123456 \\
  --context kubernetes_version=1.32 \\
  --context karpenter_version=1.0.6 \\
  --context install_external_dns=true \\
  --context hosted_zone_arns='["arn:aws:route53:::hostedzone/XXXX"]'
"""

import aws_cdk as cdk

from python_cdk.python_cdk_stack import PythonCdkStack

app = cdk.App()

PythonCdkStack(
    app,
    "PythonCdkStack",
    # Resolve the deploying account/region automatically.
    # Override by setting CDK_DEFAULT_ACCOUNT / CDK_DEFAULT_REGION or
    # passing --context env=<account>/<region>.
    env=cdk.Environment(
        account=app.node.try_get_context("account") or None,
        region=app.node.try_get_context("region") or None,
    ),
)

app.synth()
