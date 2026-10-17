"""Fine-grained assertions on the synthesized CloudFormation: the properties that matter, not a snapshot."""

import aws_cdk as cdk
import pytest
from aws_cdk.assertions import Match, Template

from ingest.stack import LAMBDA_TIMEOUT_SECONDS, IngestStack


def build(stage="dev"):
    app = cdk.App()
    stack = IngestStack(app, f"Ingest-{stage}", stage=stage, termination_protection=(stage == "prod"))
    return stack, Template.from_stack(stack)


@pytest.fixture(scope="module")
def dev():
    return build("dev")


@pytest.fixture(scope="module")
def prod():
    return build("prod")


def one(template, rtype, props=None):
    found = template.find_resources(rtype, props) if props else template.find_resources(rtype)
    assert len(found) == 1, f"expected one {rtype}, found {len(found)}"
    return next(iter(found.values()))


# --- the landing bucket --------------------------------------------------------------------------------


def landing(template):
    buckets = template.find_resources("AWS::S3::Bucket", {"Properties": {"VersioningConfiguration": {"Status": "Enabled"}}})
    assert len(buckets) == 1
    return next(iter(buckets.values()))["Properties"]


def test_the_landing_bucket_is_kms_encrypted_private_versioned_and_logged(dev):
    p = landing(dev[1])
    enc = p["BucketEncryption"]["ServerSideEncryptionConfiguration"][0]
    assert enc["ServerSideEncryptionByDefault"]["SSEAlgorithm"] == "aws:kms" and enc["BucketKeyEnabled"] is True
    assert p["PublicAccessBlockConfiguration"] == {"BlockPublicAcls": True, "BlockPublicPolicy": True,
                                                    "IgnorePublicAcls": True, "RestrictPublicBuckets": True}
    assert "DestinationBucketName" in p["LoggingConfiguration"]


def test_the_landing_bucket_expires_old_versions_and_abandoned_uploads(dev):
    rules = landing(dev[1])["LifecycleConfiguration"]["Rules"]
    assert any(r.get("NoncurrentVersionExpiration", {}).get("NoncurrentDays") == 30 for r in rules)
    assert any(r.get("AbortIncompleteMultipartUpload", {}).get("DaysAfterInitiation") == 7 for r in rules)


def test_every_bucket_refuses_plain_http(dev):
    policies = dev[1].find_resources("AWS::S3::BucketPolicy")
    assert len(policies) == 2  # landing and access-log bucket
    for pol in policies.values():
        stmts = pol["Properties"]["PolicyDocument"]["Statement"]
        assert any(s["Effect"] == "Deny" and s.get("Condition", {}).get("Bool", {}).get("aws:SecureTransport") == "false"
                   for s in stmts)


def test_every_bucket_blocks_public_access(dev):
    for b in dev[1].find_resources("AWS::S3::Bucket").values():
        assert b["Properties"]["PublicAccessBlockConfiguration"]["BlockPublicPolicy"] is True


def test_the_key_rotates(dev):
    dev[1].has_resource_properties("AWS::KMS::Key", {"EnableKeyRotation": True})


# --- queues and the function ----------------------------------------------------------------------------


def test_poison_messages_move_to_the_dlq_after_three_tries(dev):
    t = dev[1]
    queues = t.find_resources("AWS::SQS::Queue")
    assert len(queues) == 2
    main = next(q for q in queues.values() if "RedrivePolicy" in q["Properties"])
    assert main["Properties"]["RedrivePolicy"]["maxReceiveCount"] == 3
    dlq = next(q for q in queues.values() if "RedrivePolicy" not in q["Properties"])
    assert dlq["Properties"]["MessageRetentionPeriod"] == 14 * 24 * 3600


def test_visibility_timeout_is_at_least_six_times_the_function_timeout(dev):
    """AWS's guidance for SQS event sources; shorter and messages are redelivered while still being processed."""
    t = dev[1]
    main = next(q for q in t.find_resources("AWS::SQS::Queue").values() if "RedrivePolicy" in q["Properties"])
    fn = one(t, "AWS::Lambda::Function")
    assert fn["Properties"]["Timeout"] == LAMBDA_TIMEOUT_SECONDS
    assert main["Properties"]["VisibilityTimeout"] >= 6 * fn["Properties"]["Timeout"]


def test_queues_are_encrypted_and_refuse_plain_http(dev):
    t = dev[1]
    for q in t.find_resources("AWS::SQS::Queue").values():
        assert q["Properties"]["SqsManagedSseEnabled"] is True
    assert len(t.find_resources("AWS::SQS::QueuePolicy")) == 2


def test_the_function_is_modern_traced_and_capped(dev):
    p = one(dev[1], "AWS::Lambda::Function")["Properties"]
    assert p["Runtime"] == "python3.14" and p["Architectures"] == ["arm64"]
    assert p["TracingConfig"] == {"Mode": "Active"} and p["MemorySize"] == 256
    assert p["ReservedConcurrentExecutions"] == 5
    assert "BUCKET_NAME" in p["Environment"]["Variables"]


def test_the_event_source_reports_partial_batch_failures(dev):
    dev[1].has_resource_properties("AWS::Lambda::EventSourceMapping", {
        "BatchSize": 10, "FunctionResponseTypes": ["ReportBatchItemFailures"]})


def test_logs_are_kept_for_a_month_not_forever(dev):
    dev[1].has_resource_properties("AWS::Logs::LogGroup", {"RetentionInDays": 30})


def test_a_message_in_the_dlq_raises_an_alarm_that_notifies(dev):
    t = dev[1]
    alarm = one(t, "AWS::CloudWatch::Alarm")["Properties"]
    assert alarm["MetricName"] == "ApproximateNumberOfMessagesVisible" and alarm["Threshold"] == 1
    assert alarm["ComparisonOperator"] == "GreaterThanOrEqualToThreshold" and alarm["TreatMissingData"] == "notBreaching"
    assert len(alarm["AlarmActions"]) == 1
    topic = one(t, "AWS::SNS::Topic")["Properties"]
    assert "KmsMasterKeyId" in topic


# --- IAM -------------------------------------------------------------------------------------------------


def statements(template):
    for pol in template.find_resources("AWS::IAM::Policy").values():
        yield from pol["Properties"]["PolicyDocument"]["Statement"]


def test_only_xray_is_allowed_a_wildcard_resource(dev):
    for s in statements(dev[1]):
        res = s["Resource"] if isinstance(s["Resource"], list) else [s["Resource"]]
        if "*" in res:
            actions = s["Action"] if isinstance(s["Action"], list) else [s["Action"]]
            assert all(a.startswith("xray:") for a in actions), actions


def test_nothing_grants_all_actions(dev):
    for s in statements(dev[1]):
        actions = s["Action"] if isinstance(s["Action"], list) else [s["Action"]]
        assert "*" not in actions and not any(a.endswith(":*") for a in actions)


def test_the_function_can_write_to_the_landing_bucket_but_not_read_or_delete_it(dev):
    actions = {a for s in statements(dev[1]) for a in (s["Action"] if isinstance(s["Action"], list) else [s["Action"]])}
    assert "s3:PutObject*" in actions or "s3:PutObject" in actions
    assert not any(a.startswith(("s3:GetObject", "s3:DeleteObject")) for a in actions)


# --- stages differ where they should, and only there -------------------------------------------------------


def test_prod_retains_data_and_dev_can_be_torn_down(dev, prod):
    for template, policy in ((dev[1], "Delete"), (prod[1], "Retain")):
        for kind in ("AWS::S3::Bucket", "AWS::KMS::Key"):
            for r in template.find_resources(kind).values():
                assert r.get("DeletionPolicy") == policy, (kind, policy)


def test_prod_has_more_concurrency_and_termination_protection(dev, prod):
    assert one(prod[1], "AWS::Lambda::Function")["Properties"]["ReservedConcurrentExecutions"] == 20
    assert prod[0].termination_protection is True and dev[0].termination_protection is False


def test_the_two_stages_have_the_same_shape(dev, prod):
    for kind in ("AWS::S3::Bucket", "AWS::SQS::Queue", "AWS::Lambda::Function", "AWS::CloudWatch::Alarm", "AWS::SNS::Topic"):
        assert len(dev[1].find_resources(kind)) == len(prod[1].find_resources(kind)), kind


def test_resources_are_tagged_with_the_stage(dev, prod):
    dev[1].has_resource_properties("AWS::SQS::Queue", {"Tags": Match.array_with([{"Key": "stage", "Value": "dev"}])})
    prod[1].has_resource_properties("AWS::SQS::Queue", {"Tags": Match.array_with([{"Key": "stage", "Value": "prod"}])})


def test_no_hidden_helper_lambdas_or_custom_resources(dev):
    """Features like autoDeleteObjects add custom-resource Lambdas with their own permissions; none is present."""
    assert len(dev[1].find_resources("AWS::Lambda::Function")) == 1
    assert not [t for t in dev[1].to_json()["Resources"].values() if t["Type"].startswith("Custom::")]
