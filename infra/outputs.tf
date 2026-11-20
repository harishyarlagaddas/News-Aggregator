output "ecr_repository_url" {
  value = aws_ecr_repository.app.repository_url
}

output "data_bucket" {
  value = aws_s3_bucket.data.bucket
}

output "app_secret_arn" {
  value = aws_secretsmanager_secret.app.arn
}

output "log_group" {
  value = aws_cloudwatch_log_group.app.name
}

output "ses_sender_identity" {
  description = "Sender address. Click the verification link AWS emails to it before the first run"
  value       = aws_sesv2_email_identity.sender.email_identity
}

output "rerun_command" {
  description = "Start a run manually (e.g. after a failure)"
  value = join(" ", [
    "aws ecs run-task --region ${var.region}",
    "--cluster ${aws_ecs_cluster.this.name}",
    "--task-definition ${aws_ecs_task_definition.app.family}",
    "--launch-type FARGATE",
    "--network-configuration 'awsvpcConfiguration={subnets=[${join(",", aws_subnet.public[*].id)}],securityGroups=[${aws_security_group.task.id}],assignPublicIp=ENABLED}'",
  ])
}

output "dry_run_command" {
  description = "Manual run that writes nothing and sends no email (safe alongside the schedule)"
  value = join(" ", [
    "aws ecs run-task --region ${var.region}",
    "--cluster ${aws_ecs_cluster.this.name}",
    "--task-definition ${aws_ecs_task_definition.app.family}",
    "--launch-type FARGATE",
    "--network-configuration 'awsvpcConfiguration={subnets=[${join(",", aws_subnet.public[*].id)}],securityGroups=[${aws_security_group.task.id}],assignPublicIp=ENABLED}'",
    "--overrides '{\"containerOverrides\":[{\"name\":\"app\",\"command\":[\"run\",\"--dry-run\"]}]}'",
  ])
}
