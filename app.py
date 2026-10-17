#!/usr/bin/env python3
import aws_cdk as cdk
from cdk_nag import AwsSolutionsChecks

from ingest.stack import IngestStack

app = cdk.App()
stage = app.node.try_get_context("stage") or "dev"
if stage not in ("dev", "prod"):
    raise ValueError(f"stage must be dev or prod, got {stage!r}")

IngestStack(app, f"Ingest-{stage}", stage=stage, termination_protection=(stage == "prod"))

# Compliance as part of synth: any AwsSolutions finding that is not explicitly suppressed (with a reason)
# is an error, so `cdk synth` fails before anything is deployed.
cdk.Aspects.of(app).add(AwsSolutionsChecks(verbose=True))
app.synth()
