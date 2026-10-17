import json

import boto3
import pytest
from moto import mock_aws

import handler


@pytest.fixture(autouse=True)
def fake_aws(monkeypatch):
    for k, v in {"AWS_ACCESS_KEY_ID": "testing", "AWS_SECRET_ACCESS_KEY": "testing", "AWS_SESSION_TOKEN": "testing",
                 "AWS_DEFAULT_REGION": "us-east-1", "BUCKET_NAME": "landing"}.items():
        monkeypatch.setenv(k, v)


@pytest.fixture()
def s3():
    with mock_aws():
        client = boto3.client("s3")
        client.create_bucket(Bucket="landing")
        yield client


def rec(mid, body):
    return {"messageId": mid, "body": body if isinstance(body, str) else json.dumps(body)}


def keys(s3):
    return sorted(o["Key"] for o in s3.list_objects_v2(Bucket="landing").get("Contents", []))


def test_good_messages_are_written_under_their_id(s3):
    out = handler.handler({"Records": [rec("1", {"id": "a1", "payload": {"x": 1}}), rec("2", {"id": "b-2", "payload": 5})]}, s3=s3)
    assert out == {"batchItemFailures": []}
    assert keys(s3) == ["ingest/a1.json", "ingest/b-2.json"]
    assert json.loads(s3.get_object(Bucket="landing", Key="ingest/a1.json")["Body"].read())["payload"] == {"x": 1}


def test_a_bad_message_is_reported_alone_and_the_good_ones_still_land(s3):
    out = handler.handler({"Records": [rec("m1", {"id": "ok1", "payload": 1}), rec("m2", "not json"),
                                       rec("m3", {"id": "ok3", "payload": 3})]}, s3=s3)
    assert out == {"batchItemFailures": [{"itemIdentifier": "m2"}]}
    assert keys(s3) == ["ingest/ok1.json", "ingest/ok3.json"]


@pytest.mark.parametrize(
    "body",
    [{"payload": 1}, {"id": "x"}, {"id": 5, "payload": 1}, {"id": "", "payload": 1}, [1, 2], "null", "",
     {"id": "a" * 65, "payload": 1}],
)
def test_malformed_messages_are_failures_and_write_nothing(s3, body):
    out = handler.handler({"Records": [rec("m", body)]}, s3=s3)
    assert out["batchItemFailures"] == [{"itemIdentifier": "m"}] and keys(s3) == []


@pytest.mark.parametrize("evil", ["../secrets", "a/b", "..", "a b", "x\n", "é", "a.b", "a;b"])
def test_an_id_cannot_escape_the_ingest_prefix(s3, evil):
    out = handler.handler({"Records": [rec("m", {"id": evil, "payload": 1})]}, s3=s3)
    assert out["batchItemFailures"] and keys(s3) == []


def test_a_storage_error_is_a_per_record_failure_so_sqs_retries_it(s3):
    class Flaky:
        def put_object(self, **kw):
            raise RuntimeError("S3 unavailable")

    out = handler.handler({"Records": [rec("m1", {"id": "a", "payload": 1})]}, s3=Flaky())
    assert out["batchItemFailures"] == [{"itemIdentifier": "m1"}]


def test_objects_are_requested_with_kms_encryption(s3):
    seen = {}

    class Spy:
        def put_object(self, **kw):
            seen.update(kw)

    handler.handler({"Records": [rec("m", {"id": "a", "payload": 1})]}, s3=Spy())
    assert seen["ServerSideEncryption"] == "aws:kms" and seen["Bucket"] == "landing"


def test_an_empty_batch_is_fine(s3):
    assert handler.handler({"Records": []}, s3=s3) == {"batchItemFailures": []}
    assert handler.handler({}, s3=s3) == {"batchItemFailures": []}
