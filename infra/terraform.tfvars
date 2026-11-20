# Copy to terraform.tfvars and edit.
region            = "us-east-1"
schedule_timezone = "America/Los_Angeles"

email_from  = "harish.yarlagaddas@gmail.com"
email_to    = ["harish.yarlagaddas@gmail.com"]
alert_email = "harish.yarlagaddas@gmail.com"

# Verify these model IDs are enabled for your account in the Bedrock console.
llm_model = "bedrock/us.meta.llama4-scout-17b-instruct-v1:0"

# Uncomment after storing these keys in the app secret (see app_secret_arn output).
# secret_keys = ["GUARDIAN_API_KEY", "NEWSAPI_KEY"]
