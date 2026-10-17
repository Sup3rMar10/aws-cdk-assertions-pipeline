"""SQS consumer: validate each message and land it in S3.

Returns a *partial batch response*: a bad message is reported by id so SQS retries only that one (and after
the redrive limit moves it to the dead-letter queue), instead of failing the whole batch and re-running the
good messages.
"""

from __future__ import annotations

import json
import os
import re

import boto3

_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")


def parse(body: str) -> dict:
    msg = json.loads(body)
    if not isinstance(msg, dict) or "payload" not in msg:
        raise ValueError("message must be an object with a payload")
    # The id becomes part of an object key, so it must not be able to contain '/' or '..'.
    if not isinstance(msg.get("id"), str) or not _ID.fullmatch(msg["id"]):
        raise ValueError("message id must be 1-64 characters of letters, digits, '_' or '-'")
    return msg


def handler(event, context=None, s3=None):
    s3 = s3 or boto3.client("s3")
    bucket = os.environ["BUCKET_NAME"]
    failures = []
    for record in event.get("Records", []):
        try:
            msg = parse(record["body"])
            s3.put_object(Bucket=bucket, Key=f"ingest/{msg['id']}.json", Body=json.dumps(msg).encode(),
                          ServerSideEncryption="aws:kms", ContentType="application/json")
        except Exception:  # noqa: BLE001 - any failure for one record is reported for that record only
            failures.append({"itemIdentifier": record["messageId"]})
    return {"batchItemFailures": failures}
