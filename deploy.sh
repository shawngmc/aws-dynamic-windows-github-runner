#!/usr/bin/env bash
# Deploys (or updates) the stack and uploads the runner bootstrap script.
# Needs bash and AWS CLI v2 with credentials (AWS CloudShell has both).
#
#   ./deploy.sh VpcId=vpc-0123 SubnetId=subnet-0123 AllowedRepositories=owner/repo1,owner/repo2 [Param=Value ...]
#
# Environment: STACK_NAME (default gh-runner), AWS_REGION (default: the CLI's configured region),
# ARTIFACTS_BUCKET (where the Lambda code is uploaded; created if missing).
# On later runs, parameters you don't pass keep their current values.
set -euo pipefail
cd "$(dirname "$0")"

STACK_NAME="${STACK_NAME:-gh-runner}"
REGION="${AWS_REGION:-$(aws configure get region || true)}"
[ -n "$REGION" ] || { echo "Set AWS_REGION." >&2; exit 1; }
ACCOUNT="$(aws sts get-caller-identity --query Account --output text)"
ARTIFACTS_BUCKET="${ARTIFACTS_BUCKET:-${STACK_NAME}-artifacts-${ACCOUNT}-${REGION}}"

if ! aws s3api head-bucket --bucket "$ARTIFACTS_BUCKET" --region "$REGION" 2>/dev/null; then
  echo "Creating artifacts bucket $ARTIFACTS_BUCKET"
  if [ "$REGION" = "us-east-1" ]; then
    aws s3api create-bucket --bucket "$ARTIFACTS_BUCKET" --region "$REGION" >/dev/null
  else
    aws s3api create-bucket --bucket "$ARTIFACTS_BUCKET" --region "$REGION" \
      --create-bucket-configuration LocationConstraint="$REGION" >/dev/null
  fi
  aws s3api put-public-access-block --bucket "$ARTIFACTS_BUCKET" --region "$REGION" \
    --public-access-block-configuration BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true
fi

mkdir -p .build
aws cloudformation package --region "$REGION" --template-file template.yaml \
  --s3-bucket "$ARTIFACTS_BUCKET" --s3-prefix "$STACK_NAME" --output-template-file .build/packaged.yaml >/dev/null

overrides=()
[ $# -gt 0 ] && overrides=(--parameter-overrides "$@")
aws cloudformation deploy --region "$REGION" --stack-name "$STACK_NAME" \
  --template-file .build/packaged.yaml --capabilities CAPABILITY_IAM \
  --no-fail-on-empty-changeset "${overrides[@]}"

output() {
  aws cloudformation describe-stacks --region "$REGION" --stack-name "$STACK_NAME" \
    --query "Stacks[0].Outputs[?OutputKey=='$1'].OutputValue" --output text
}
BUCKET="$(output RunnerBucket)"
aws s3 cp runner/bootstrap.ps1 "s3://$BUCKET/bootstrap/bootstrap.ps1" --region "$REGION" >/dev/null
echo "Uploaded runner/bootstrap.ps1 to s3://$BUCKET/bootstrap/"

echo
echo "Webhook URL:     $(output WebhookUrl)"
echo "Webhook secret:  aws secretsmanager get-secret-value --region $REGION --secret-id $(output WebhookSecretArn) --query SecretString --output text"
echo "GitHub token:    aws secretsmanager put-secret-value --region $REGION --secret-id $(output GitHubTokenSecretArn) --secret-string '<token>'"
echo "Runner bucket:   $BUCKET"
echo "runs-on:         $(output RunsOn)"
