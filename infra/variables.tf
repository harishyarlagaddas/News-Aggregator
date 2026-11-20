variable "project" {
  description = "Name prefix for all resources"
  type        = string
  default     = "news-aggregator"
}

variable "region" {
  description = "AWS region (must offer Bedrock with the chosen models, and SES)"
  type        = string
  default     = "us-east-1"
}

variable "image_tag" {
  description = "Container image tag in ECR to run"
  type        = string
  default     = "latest"
}

variable "schedule_expression" {
  description = "EventBridge Scheduler expression for the daily run"
  type        = string
  default     = "cron(30 6 * * ? *)" # 06:30 every day
}

variable "schedule_timezone" {
  description = "IANA timezone for the schedule"
  type        = string
  default     = "America/Los_Angeles"
}

variable "task_cpu" {
  description = "Fargate task CPU units (2048 = 2 vCPU)"
  type        = number
  default     = 2048
}

variable "task_memory" {
  description = "Fargate task memory in MiB"
  type        = number
  default     = 4096
}

variable "email_from" {
  description = "Sender email address, verified in SES as an email-address identity (e.g. a Gmail address)"
  type        = string
}

variable "email_to" {
  description = "Briefing recipients"
  type        = list(string)
}

variable "alert_email" {
  description = "Address that receives failure alerts (confirm the SNS subscription email)"
  type        = string
}

variable "llm_model" {
  description = "Bedrock model for the Writer crew, in CrewAI/LiteLLM form"
  type        = string
  default     = "bedrock/us.anthropic.claude-sonnet-4-5-20250929-v1:0"
}

variable "embedding_backend" {
  description = "local (model baked into the image) or bedrock (Titan embeddings)"
  type        = string
  default     = "local"
}

variable "secret_keys" {
  description = <<-EOT
    Keys inside the app secret to expose as environment variables, e.g. ["GUARDIAN_API_KEY"].
    Populate the secret in Secrets Manager before adding a key here, or the task will fail to start.
  EOT
  type        = list(string)
  default     = []
}

variable "index_version_retention_days" {
  description = "Days to keep noncurrent versions of S3 objects (index rollback window)"
  type        = number
  default     = 30
}

variable "briefing_retention_days" {
  description = "Days to keep archived briefings (0 = keep forever)"
  type        = number
  default     = 0
}

variable "vpc_cidr" {
  description = "CIDR for the small public VPC the task runs in"
  type        = string
  default     = "10.42.0.0/24"
}
