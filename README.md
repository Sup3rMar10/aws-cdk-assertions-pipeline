# aws-cdk-assertions-pipeline

![ci](https://github.com/Sup3rMar10/aws-cdk-assertions-pipeline/actions/workflows/ci.yml/badge.svg)

An AWS CDK (Python) app whose pipeline treats **infrastructure as testable code**: fine-grained assertions on the synthesized CloudFormation, **cdk-nag compliance checks that fail the build**, the real CDK CLI synthesizing both stages, and `cfn-lint` on the generated templates, all with no AWS credentials.

> **What is verified, and what is not.** 45 tests pass locally on Python 3.13 (about a minute, mostly synthesis), covering the stack, the cdk-nag AwsSolutions pack and the Lambda handler (against moto, an in-memory S3). `cfn-lint` finds no errors in the synthesized templates. **Nothing here has ever been deployed**: there is no AWS account behind this repository, so `cdk diff` and `cdk deploy` have never run, and the deploy workflow is skipped unless you set `AWS_DEPLOY_ROLE_ARN`.

## What it builds (`ingest/stack.py`)

SQS in, Lambda, S3 out, with failure handling designed in:

```
SQS queue --(batch of 10, partial-batch responses)--> Lambda --> S3 landing bucket (KMS, versioned, logged)
   |  after 3 failed receives
   v
dead-letter queue --> CloudWatch alarm (any message) --> SNS topic (KMS)
```

`dev` and `prod` come from one stack and differ only where they should: prod retains the bucket, key and logs, reserves 20 Lambda concurrency instead of 5, and has termination protection.

## Four layers of checks, cheapest first

| Layer | What it proves | Where |
|---|---|---|
| **Template assertions** | the specific properties that matter: bucket KMS-encrypted, public access blocked, TLS-only policies on both buckets and both queues, key rotation, versioning with expiry of old versions and abandoned uploads, redrive after 3 tries, **queue visibility timeout at least 6x the function timeout** (AWS's guidance for SQS event sources), partial-batch reporting, alarm on the DLQ wired to a notifying topic, no wildcard actions, **wildcard resources only for X-Ray**, the function can write to the bucket but not read or delete from it | `tests/test_stack.py` |
| **cdk-nag** (AwsSolutions pack) | no compliance finding that is not suppressed. **A negative test builds a deliberately bad stack and asserts nag complains** (`AwsSolutions-S1`, `S10`), so a silent nag cannot make the real test meaningless. Every suppression must carry a real reason (also tested) | `tests/test_nag.py` |
| **The CDK CLI in CI** | `cdk synth` for dev and prod exactly as a deploy would, where error annotations fail the command | `ci.yml` `synth` job |
| **cfn-lint on the output** | the generated CloudFormation is valid, independent of CDK | `ci.yml` |

Assertions instead of a snapshot test: a snapshot fails on every harmless change and gets blindly updated; assertions fail only when a property someone cared about changes.

## Things the checks found while building it

- **A real bug, found by a test:** the Lambda validates message ids before using them in an S3 key. My regex ended in `$`, which in Python matches *before a trailing newline*, so the id `"x\n"` passed. The test `x\n` failed; the fix is `fullmatch`.
- cdk-nag flagged **Python 3.13 as not the latest Lambda runtime** (`AwsSolutions-L1`), so the stack uses 3.14. (Whether you *want* the newest runtime is a separate decision; the build now forces a conscious one.)
- cdk-nag **3.x removed the `NagSuppressions` API** this repo uses, so `requirements.txt` pins the 2.x line, with a comment saying why.
- `app.synth()` does **not** raise on nag findings; they are annotations that the CDK CLI turns into failures. That is why the tests read the annotations themselves rather than assuming a synth error.

## Honest limits

- **Never deployed.** CloudFormation can still reject something that synthesizes and lints cleanly (a quota, a name clash, an eventual-consistency ordering). `cdk diff` against a real account is the missing step.
- **Four suppressions** are in the code, each with a reason: the access-log bucket cannot log to itself (`S1`), the main queue's dead-letter queue cannot have one (`SQS3`), and two for IAM action wildcards that CDK's grant helpers and X-Ray require (`IAM5`). cdk-nag is a rule pack, not a security review; passing it is not the same as being secure.
- The Lambda is tested against moto (an emulation), and the alarm, DLQ redrive and partial-batch behaviour are asserted on the *template*, never exercised by real messages.
- `dev` uses `RemovalPolicy.DESTROY` without `autoDeleteObjects` (which would add a custom-resource Lambda; a test asserts there is none), so destroying a dev stack that holds objects fails until the bucket is emptied.
- One stack, one account, one region: no cross-account pipeline, no CDK Pipelines construct, no bootstrap or asset publishing was exercised.
- cfn-lint ignores two warnings (`W3045`, `W3005`); the reasons are in `ci.yml`.

## Run it

```
python -m pip install -r requirements-dev.txt
python -m pytest                      # about a minute
npm install -g aws-cdk
cdk synth -c stage=prod               # needs no AWS credentials
```
