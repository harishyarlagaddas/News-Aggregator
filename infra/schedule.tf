# EventBridge Scheduler starts one Fargate task per day.
# Its retry policy covers failures to START the task (e.g. capacity, throttling).
# A task that starts and then exits non-zero triggers the SNS alert in monitoring.tf;
# re-run it with the `rerun_command` output.
resource "aws_scheduler_schedule" "daily" {
  name                         = "${local.name}-daily"
  schedule_expression          = var.schedule_expression
  schedule_expression_timezone = var.schedule_timezone

  flexible_time_window { mode = "OFF" }

  target {
    arn      = aws_ecs_cluster.this.arn
    role_arn = aws_iam_role.scheduler.arn

    ecs_parameters {
      task_definition_arn = aws_ecs_task_definition.app.arn
      launch_type         = "FARGATE"
      task_count          = 1

      network_configuration {
        subnets          = aws_subnet.public[*].id
        security_groups  = [aws_security_group.task.id]
        assign_public_ip = true
      }
    }

    retry_policy {
      maximum_retry_attempts       = 2
      maximum_event_age_in_seconds = 3600
    }
  }
}
