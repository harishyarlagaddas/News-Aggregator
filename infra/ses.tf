# SES email-address identity for the sender. No domain or DNS records are needed: AWS
# emails a verification link to var.email_from, and sending works once it is clicked.
# New SES accounts are in the sandbox (can only send to verified addresses) until you
# request production access in the SES console.
resource "aws_sesv2_email_identity" "sender" {
  email_identity = var.email_from
}

# While in the sandbox, each recipient must be verified too. AWS emails each a link.
# The sender is skipped here because it is already verified above.
resource "aws_sesv2_email_identity" "recipients" {
  for_each       = setsubtract(toset(var.email_to), [var.email_from])
  email_identity = each.value
}
