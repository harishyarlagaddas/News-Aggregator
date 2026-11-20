data "aws_caller_identity" "current" {}
data "aws_availability_zones" "available" { state = "available" }

locals {
  name       = var.project
  account_id = data.aws_caller_identity.current.account_id
  azs        = slice(data.aws_availability_zones.available.names, 0, 2)
}

# -----------------------------------------------------------------------------
# Network: a tiny VPC with two public subnets. The task gets a public IP and has no
# inbound rules, which avoids a NAT gateway (~$32/month, more than everything else).
# -----------------------------------------------------------------------------
resource "aws_vpc" "this" {
  cidr_block           = var.vpc_cidr
  enable_dns_support   = true
  enable_dns_hostnames = true
  tags                 = { Name = local.name }
}

resource "aws_internet_gateway" "this" {
  vpc_id = aws_vpc.this.id
  tags   = { Name = local.name }
}

resource "aws_subnet" "public" {
  count                   = length(local.azs)
  vpc_id                  = aws_vpc.this.id
  availability_zone       = local.azs[count.index]
  cidr_block              = cidrsubnet(var.vpc_cidr, 2, count.index)
  map_public_ip_on_launch = true
  tags                    = { Name = "${local.name}-public-${count.index}" }
}

resource "aws_route_table" "public" {
  vpc_id = aws_vpc.this.id
  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.this.id
  }
  tags = { Name = "${local.name}-public" }
}

resource "aws_route_table_association" "public" {
  count          = length(aws_subnet.public)
  subnet_id      = aws_subnet.public[count.index].id
  route_table_id = aws_route_table.public.id
}

resource "aws_security_group" "task" {
  name        = "${local.name}-task"
  description = "Outbound only for the news aggregator task"
  vpc_id      = aws_vpc.this.id

  egress {
    description = "HTTPS/HTTP to news sources and AWS APIs"
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

# -----------------------------------------------------------------------------
# S3: FAISS index + metadata, HTTP cache, archived briefings
# -----------------------------------------------------------------------------
resource "aws_s3_bucket" "data" {
  bucket_prefix = "${local.name}-"
  force_destroy = false
}

resource "aws_s3_bucket_versioning" "data" {
  bucket = aws_s3_bucket.data.id
  versioning_configuration { status = "Enabled" }
}

resource "aws_s3_bucket_public_access_block" "data" {
  bucket                  = aws_s3_bucket.data.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_server_side_encryption_configuration" "data" {
  bucket = aws_s3_bucket.data.id
  rule {
    apply_server_side_encryption_by_default { sse_algorithm = "AES256" }
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "data" {
  bucket = aws_s3_bucket.data.id

  rule {
    id     = "expire-old-versions"
    status = "Enabled"
    filter {}
    noncurrent_version_expiration {
      noncurrent_days = var.index_version_retention_days
    }
  }

  dynamic "rule" {
    for_each = var.briefing_retention_days > 0 ? [1] : []
    content {
      id     = "expire-briefings"
      status = "Enabled"
      filter { prefix = "prod/briefings/" }
      expiration { days = var.briefing_retention_days }
    }
  }
}

# -----------------------------------------------------------------------------
# ECR
# -----------------------------------------------------------------------------
resource "aws_ecr_repository" "app" {
  name                 = local.name
  image_tag_mutability = "MUTABLE"
  image_scanning_configuration { scan_on_push = true }
}

resource "aws_ecr_lifecycle_policy" "app" {
  repository = aws_ecr_repository.app.name
  policy = jsonencode({
    rules = [{
      rulePriority = 1
      description  = "Keep the 5 most recent images"
      selection    = { tagStatus = "any", countType = "imageCountMoreThan", countNumber = 5 }
      action       = { type = "expire" }
    }]
  })
}

# -----------------------------------------------------------------------------
# Secrets Manager: optional news API keys (Bedrock uses the task role, no key needed).
# Terraform creates the container only; put the JSON value in yourself, e.g.
#   aws secretsmanager put-secret-value --secret-id <arn> \
#     --secret-string '{"GUARDIAN_API_KEY":"...","NEWSAPI_KEY":"..."}'
# -----------------------------------------------------------------------------
resource "aws_secretsmanager_secret" "app" {
  name_prefix = "${local.name}-"
  description = "API keys for optional news sources"
}
