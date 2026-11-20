resource "aws_sns_topic" "alerts" {
  name = "${local.name}-alerts"
}

resource "aws_sns_topic_subscription" "alert_email" {
  topic_arn = aws_sns_topic.alerts.arn
  protocol  = "email"
  endpoint  = var.alert_email
}

resource "aws_sns_topic_policy" "alerts" {
  arn = aws_sns_topic.alerts.arn
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid       = "AllowEventBridge"
        Effect    = "Allow"
        Principal = { Service = "events.amazonaws.com" }
        Action    = "sns:Publish"
        Resource  = aws_sns_topic.alerts.arn
      },
      {
        Sid       = "AllowCloudWatchAlarms"
        Effect    = "Allow"
        Principal = { Service = "cloudwatch.amazonaws.com" }
        Action    = "sns:Publish"
        Resource  = aws_sns_topic.alerts.arn
        Condition = { StringEquals = { "aws:SourceAccount" = local.account_id } }
      },
    ]
  })
}

# Alert 1: the task stopped with a non-zero exit code (or never got its container running).
resource "aws_cloudwatch_event_rule" "task_failed" {
  name        = "${local.name}-task-failed"
  description = "News aggregator task stopped unsuccessfully"
  event_pattern = jsonencode({
    source        = ["aws.ecs"]
    "detail-type" = ["ECS Task State Change"]
    detail = {
      clusterArn = [aws_ecs_cluster.this.arn]
      lastStatus = ["STOPPED"]
      containers = { exitCode = [{ "anything-but" = 0 }] }
    }
  })
}

resource "aws_cloudwatch_event_target" "task_failed" {
  rule      = aws_cloudwatch_event_rule.task_failed.name
  target_id = "sns"
  arn       = aws_sns_topic.alerts.arn
}

# Alert 2: no "briefing_sent" log line in the last 24 hours (catches silent misses,
# e.g. the schedule never fired).
resource "aws_cloudwatch_log_metric_filter" "briefing_sent" {
  name           = "${local.name}-briefing-sent"
  log_group_name = aws_cloudwatch_log_group.app.name
  pattern        = "{ $.event = \"briefing_sent\" }"

  metric_transformation {
    name          = "BriefingsSent"
    namespace     = "NewsAggregator"
    value         = "1"
    default_value = "0"
  }
}

resource "aws_cloudwatch_metric_alarm" "no_briefing" {
  alarm_name          = "${local.name}-no-briefing-24h"
  alarm_description   = "No daily briefing was sent in the last 24 hours"
  namespace           = "NewsAggregator"
  metric_name         = "BriefingsSent"
  statistic           = "Sum"
  period              = 86400
  evaluation_periods  = 1
  comparison_operator = "LessThanThreshold"
  threshold           = 1
  treat_missing_data  = "breaching"
  alarm_actions       = [aws_sns_topic.alerts.arn]
  ok_actions          = [aws_sns_topic.alerts.arn]
}
