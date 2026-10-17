from aws_cdk import CfnOutput, Duration, RemovalPolicy, Stack, Tags
from aws_cdk import aws_cloudwatch as cw
from aws_cdk import aws_cloudwatch_actions as cw_actions
from aws_cdk import aws_iam as iam
from aws_cdk import aws_kms as kms
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_lambda_event_sources as sources
from aws_cdk import aws_logs as logs
from aws_cdk import aws_s3 as s3
from aws_cdk import aws_sns as sns
from aws_cdk import aws_sqs as sqs
from cdk_nag import NagSuppressions
from constructs import Construct

LAMBDA_TIMEOUT_SECONDS = 10


class IngestStack(Stack):
    """SQS in, Lambda, S3 out, with a dead-letter queue and an alarm when anything lands in it."""

    def __init__(self, scope: Construct, construct_id: str, *, stage: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)
        prod = stage == "prod"
        removal = RemovalPolicy.RETAIN if prod else RemovalPolicy.DESTROY

        key = kms.Key(self, "DataKey", enable_key_rotation=True, removal_policy=removal,
                      description=f"Encrypts the {stage} ingest data and its alarm topic")
        # CloudWatch must be able to use the key to publish to the encrypted alarm topic.
        key.add_to_resource_policy(iam.PolicyStatement(
            principals=[iam.ServicePrincipal("cloudwatch.amazonaws.com")],
            actions=["kms:Decrypt", "kms:GenerateDataKey*"], resources=["*"]))

        log_bucket = s3.Bucket(
            self, "AccessLogs", enforce_ssl=True, block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            encryption=s3.BucketEncryption.S3_MANAGED, removal_policy=removal,
            object_ownership=s3.ObjectOwnership.BUCKET_OWNER_PREFERRED,
            lifecycle_rules=[s3.LifecycleRule(expiration=Duration.days(90))])
        NagSuppressions.add_resource_suppressions(log_bucket, [{
            "id": "AwsSolutions-S1",
            "reason": "This is the access-log bucket itself; logging it would need another bucket, forever."}])

        bucket = s3.Bucket(
            self, "Landing", encryption=s3.BucketEncryption.KMS, encryption_key=key, bucket_key_enabled=True,
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL, enforce_ssl=True, versioned=True,
            server_access_logs_bucket=log_bucket, server_access_logs_prefix="landing/", removal_policy=removal,
            lifecycle_rules=[s3.LifecycleRule(noncurrent_version_expiration=Duration.days(30),
                                              abort_incomplete_multipart_upload_after=Duration.days(7))])

        dlq = sqs.Queue(self, "Dlq", encryption=sqs.QueueEncryption.SQS_MANAGED, enforce_ssl=True,
                        retention_period=Duration.days(14))
        queue = sqs.Queue(
            self, "Queue", encryption=sqs.QueueEncryption.SQS_MANAGED, enforce_ssl=True,
            # AWS guidance for Lambda event sources: visibility timeout of at least 6 x the function timeout.
            visibility_timeout=Duration.seconds(LAMBDA_TIMEOUT_SECONDS * 6),
            dead_letter_queue=sqs.DeadLetterQueue(max_receive_count=3, queue=dlq))

        log_group = logs.LogGroup(self, "ConsumerLogs", retention=logs.RetentionDays.ONE_MONTH, removal_policy=removal)
        role = iam.Role(self, "ConsumerRole", assumed_by=iam.ServicePrincipal("lambda.amazonaws.com"))
        log_group.grant_write(role)
        role.add_to_policy(iam.PolicyStatement(
            actions=["xray:PutTraceSegments", "xray:PutTelemetryRecords"], resources=["*"]))

        fn = lambda_.Function(
            self, "Consumer", runtime=lambda_.Runtime.PYTHON_3_14, architecture=lambda_.Architecture.ARM_64,
            handler="handler.handler", code=lambda_.Code.from_asset("lambda"), role=role, log_group=log_group,
            timeout=Duration.seconds(LAMBDA_TIMEOUT_SECONDS), memory_size=256, tracing=lambda_.Tracing.ACTIVE,
            reserved_concurrent_executions=20 if prod else 5, environment={"BUCKET_NAME": bucket.bucket_name})
        bucket.grant_put(fn)
        fn.add_event_source(sources.SqsEventSource(queue, batch_size=10, report_batch_item_failures=True))

        topic = sns.Topic(self, "AlarmTopic", master_key=key, enforce_ssl=True)
        alarm = dlq.metric_approximate_number_of_messages_visible(period=Duration.minutes(1)).create_alarm(
            self, "DlqNotEmpty", threshold=1, evaluation_periods=1,
            comparison_operator=cw.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
            treat_missing_data=cw.TreatMissingData.NOT_BREACHING,
            alarm_description="A message failed 3 times and was moved to the dead-letter queue")
        alarm.add_alarm_action(cw_actions.SnsAction(topic))

        NagSuppressions.add_resource_suppressions(role, [{
            "id": "AwsSolutions-IAM5",
            "reason": "X-Ray write actions do not support resource-level permissions; log permissions are scoped to this log group."}],
            apply_to_children=True)
        NagSuppressions.add_resource_suppressions(fn.role, [{
            "id": "AwsSolutions-IAM5",
            "reason": "The CDK grant helpers use action wildcards (s3:Abort*, kms:GenerateDataKey*) scoped to this bucket and key."}],
            apply_to_children=True)
        NagSuppressions.add_resource_suppressions(queue, [{
            "id": "AwsSolutions-SQS3", "reason": "This queue has a dead-letter queue; the DLQ itself cannot have one."}])

        Tags.of(self).add("app", "ingest")
        Tags.of(self).add("stage", stage)
        CfnOutputs = {"QueueUrl": queue.queue_url, "DlqUrl": dlq.queue_url, "BucketName": bucket.bucket_name}
        for name, value in CfnOutputs.items():
            CfnOutput(self, name, value=value)
