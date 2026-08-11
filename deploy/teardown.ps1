# Tear down local Floci deployment (Lambda, S3, EventBridge, Docker container).
# Deletes all provisioned AWS resources from local Floci and stops the container.
# Use -RemoveVolumes to also remove persistent data.

param(
    [switch]$RemoveVolumes
)

$ErrorActionPreference = "Continue"

# Add venv to PATH (aws CLI is installed there)
$venvScripts = "C:\Users\jacks\Documents\ai-ready\.venv\Scripts"
if (Test-Path $venvScripts) {
    $env:PATH = "$venvScripts;$env:PATH"
}

Write-Host "=== AI Knowledge Maintenance - Teardown ===" -ForegroundColor Cyan
Write-Host ""

# Set AWS env vars for local mode
$env:AWS_ENDPOINT_URL = "http://localhost:4566"
$env:AWS_DEFAULT_REGION = "us-east-1"
$env:AWS_ACCESS_KEY_ID = "test"
$env:AWS_SECRET_ACCESS_KEY = "test"

# 1. Delete EventBridge rule
Write-Host "[1/4] Removing EventBridge rule..." -ForegroundColor Yellow
aws events remove-targets --rule assess-schedule --ids "1" --endpoint-url http://localhost:4566 2>$null
aws events delete-rule --name assess-schedule --endpoint-url http://localhost:4566 2>$null
Write-Host "  Done." -ForegroundColor Green

# 2. Delete Lambda function
Write-Host "[2/4] Deleting Lambda function..." -ForegroundColor Yellow
aws lambda delete-function --function-name ai-knowledge-assess --endpoint-url http://localhost:4566 2>$null
Write-Host "  Done." -ForegroundColor Green

# 3. Delete S3 bucket
Write-Host "[3/4] Deleting S3 bucket..." -ForegroundColor Yellow
aws s3 rb s3://knowledge-base --force --endpoint-url http://localhost:4566 2>$null
Write-Host "  Done." -ForegroundColor Green

# 4. Stop Floci
Write-Host "[4/4] Stopping Floci container..." -ForegroundColor Yellow
if ($RemoveVolumes) {
    docker compose -f deploy/compose.yaml down -v
    Write-Host "  Floci stopped and volumes removed." -ForegroundColor Green
} else {
    docker compose -f deploy/compose.yaml down
    Write-Host "  Floci stopped (volumes preserved)." -ForegroundColor Green
}

# Clean up package artifacts
if (Test-Path "deploy/function.zip") { Remove-Item "deploy/function.zip" -Force }
if (Test-Path "lambda_package") { Remove-Item "lambda_package" -Recurse -Force }

Write-Host ""
Write-Host "Teardown Complete" -ForegroundColor Green
