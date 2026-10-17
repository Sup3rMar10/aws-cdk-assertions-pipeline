"""cdk-nag's AwsSolutions pack as a test: compliance findings fail the build unless suppressed with a reason."""

import aws_cdk as cdk
import pytest
from aws_cdk import Aspects, Stack
from aws_cdk import aws_s3 as s3
from aws_cdk.assertions import Annotations, Match
from cdk_nag import AwsSolutionsChecks

from ingest.stack import IngestStack

ANY_NAG_ERROR = Match.string_like_regexp("AwsSolutions-.*")


def nag_errors(build):
    app = cdk.App()
    Aspects.of(app).add(AwsSolutionsChecks())
    stack = build(app)
    app.synth()
    return Annotations.from_stack(stack).find_error("*", ANY_NAG_ERROR)


@pytest.mark.parametrize("stage", ["dev", "prod"])
def test_the_stack_has_no_unsuppressed_compliance_errors(stage):
    errors = nag_errors(lambda app: IngestStack(app, "T", stage=stage))
    assert errors == [], [e.entry.data for e in errors]


def test_cdk_nag_really_does_catch_a_non_compliant_stack():
    """If this ever passes with zero errors, the test above would be meaningless."""
    def bad(app):
        stack = Stack(app, "Bad")
        s3.Bucket(stack, "Open")  # no TLS enforcement, no access logs
        return stack

    errors = nag_errors(bad)
    assert len(errors) >= 2
    text = " ".join(str(e.entry.data) for e in errors)
    assert "AwsSolutions-S1" in text and "AwsSolutions-S10" in text


def test_every_suppression_in_the_stack_carries_a_reason():
    import re
    from pathlib import Path

    src = Path(__file__).resolve().parent.parent / "ingest" / "stack.py"
    text = src.read_text(encoding="utf-8")
    ids = re.findall(r'"id": "(AwsSolutions-[A-Z0-9]+)"', text)
    reasons = re.findall(r'"reason":\s*"([^"]{20,})"', text)
    assert ids and len(reasons) == len(ids), "each suppression needs a real (20+ character) reason"
