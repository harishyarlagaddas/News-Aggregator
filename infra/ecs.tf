resource "aws_ecs_cluster" "this" {
  name = local.name
  setting {
    name  = "containerInsights"
    value = "disabled" # enable if you want per-task metrics (extra cost)
  }
}

resource "aws_cloudwatch_log_group" "app" {
  name              = "/ecs/${local.name}"
  retention_in_days = 30
}

resource "aws_ecs_task_definition" "app" {
  family                   = local.name
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = var.task_cpu
  memory                   = var.task_memory
  execution_role_arn       = aws_iam_role.execution.arn
  task_role_arn            = aws_iam_role.task.arn

  runtime_platform {
    operating_system_family = "LINUX"
    cpu_architecture        = "X86_64"
  }

  ephemeral_storage { size_in_gib = 21 }

  container_definitions = jsonencode([{
    name      = "app"
    image     = "${aws_ecr_repository.app.repository_url}:${var.image_tag}"
    essential = true
    command   = ["run"]

    environment = [
      { name = "NEWSAGG_STORAGE_BACKEND", value = "s3" },
      { name = "NEWSAGG_S3_BUCKET", value = aws_s3_bucket.data.bucket },
      { name = "NEWSAGG_S3_PREFIX", value = "prod" },
      { name = "NEWSAGG_EMAIL_BACKEND", value = "ses" },
      { name = "NEWSAGG_EMAIL_FROM", value = var.email_from },
      { name = "NEWSAGG_EMAIL_TO", value = join(",", var.email_to) },
      { name = "NEWSAGG_LLM_MODEL", value = var.llm_model },
      { name = "NEWSAGG_EMBEDDING_BACKEND", value = var.embedding_backend },
      { name = "NEWSAGG_LOG_FORMAT", value = "json" },
      { name = "AWS_REGION", value = var.region },
      { name = "AWS_DEFAULT_REGION", value = var.region },
      { name = "AWS_REGION_NAME", value = var.region }, # read by LiteLLM for Bedrock
    ]

    secrets = [
      for key in var.secret_keys : {
        name      = key
        valueFrom = "${aws_secretsmanager_secret.app.arn}:${key}::"
      }
    ]

    logConfiguration = {
      logDriver = "awslogs"
      options = {
        awslogs-group         = aws_cloudwatch_log_group.app.name
        awslogs-region        = var.region
        awslogs-stream-prefix = "run"
      }
    }
  }])
}
