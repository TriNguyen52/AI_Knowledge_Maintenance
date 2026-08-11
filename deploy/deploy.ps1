# Deploy AI Knowledge Maintenance to local Floci (free, no AWS account needed).
# Starts Floci, provisions S3 bucket, Lambda function, and EventBridge rule.
# The same lambda_handler.py code runs unchanged - only the endpoint URL differs.
# Prerequisites: Docker Desktop, Python 3.12, AWS CLI, CockroachDB cluster, Groq API key
# Cost: $0 - everything runs locally via Docker

$ErrorActionPreference = "Stop"

# Add venv to PATH (aws CLI is installed there)
$venvScripts = "C:\Users\jacks\Documents\ai-ready\.venv\Scripts"
if (Test-Path $venvScripts) {
    $env:PATH = "$venvScripts;$env:PATH"
}

Write-Host "=== AI Knowledge Maintenance - Local Deployment ===" -ForegroundColor Cyan
Write-Host ""

# 1. Start Floci
Write-Host "[1/7] Starting Floci (local AWS emulator)..." -ForegroundColor Yellow
docker compose -f deploy/compose.yaml up -d
if ($LASTEXITCODE -ne 0) {
    Write-Host 'ERROR: Failed to start Floci. Is Docker Desktop running?' -ForegroundColor Red
    exit 1
}

# 2. Wait for Floci to be ready
Write-Host "[2/7] Waiting for Floci to be ready..." -ForegroundColor Yellow
$retries = 30
$ready = $false
while ($retries -gt 0 -and -not $ready) {
    Start-Sleep -Seconds 2
    try {
        $response = Invoke-WebRequest -Uri "http://localhost:4566/_localstack/health" -UseBasicParsing -TimeoutSec 3
        if ($response.StatusCode -eq 200) {
            $ready = $true
        }
    } catch {
        $retries--
        Write-Host "  Waiting... $retries retries left"
    }
}
if (-not $ready) {
    Write-Host 'ERROR: Floci did not become ready in 60 seconds.' -ForegroundColor Red
    exit 1
}
Write-Host "  Floci is ready." -ForegroundColor Green

# 3. Set AWS env vars for local mode
Write-Host "[3/7] Configuring AWS environment for local mode..." -ForegroundColor Yellow
$env:AWS_ENDPOINT_URL = "http://localhost:4566"
$env:AWS_DEFAULT_REGION = "us-east-1"
$env:AWS_ACCESS_KEY_ID = "test"
$env:AWS_SECRET_ACCESS_KEY = "test"
Write-Host "  AWS_ENDPOINT_URL = $env:AWS_ENDPOINT_URL"
Write-Host "  AWS_DEFAULT_REGION = $env:AWS_DEFAULT_REGION"

# 4. Create S3 knowledge bucket
Write-Host "[4/7] Creating S3 knowledge bucket..." -ForegroundColor Yellow
aws s3 mb s3://knowledge-base --endpoint-url http://localhost:4566 2>$null
if ($LASTEXITCODE -ne 0) {
    Write-Host "  Bucket may already exist - continuing." -ForegroundColor DarkYellow
} else {
    Write-Host "  Bucket created: s3://knowledge-base" -ForegroundColor Green
}

# Upload first 100 FastAPI docs to S3 (docs100/ prefix for demo)
Write-Host "  Uploading first 100 FastAPI docs to S3 (docs100/ prefix)..." -ForegroundColor Yellow
if (Test-Path "fastapi/docs/en/docs") {
    $docsRoot = (Resolve-Path "fastapi/docs/en/docs").Path
    $mdFiles = Get-ChildItem -Path $docsRoot -Filter "*.md" -Recurse | Sort-Object FullName | Select-Object -First 100
    $uploaded = 0
    foreach ($f in $mdFiles) {
        $relPath = $f.FullName.Substring($docsRoot.Length + 1).Replace("\", "/")
        $s3Key = "docs100/$relPath"
        aws s3 cp $f.FullName "s3://knowledge-base/$s3Key" --endpoint-url http://localhost:4566 2>$null
        $uploaded++
        if ($uploaded % 20 -eq 0) {
            Write-Host "    Uploaded $uploaded/100..." -ForegroundColor DarkGray
        }
    }
    Write-Host "  Uploaded $uploaded files to s3://knowledge-base/docs100/" -ForegroundColor Green
} else {
    Write-Host "  WARNING: fastapi/docs/en/docs not found - skipping upload." -ForegroundColor DarkYellow
}

# 5. Install Linux-compatible dependencies via Docker
Write-Host "[5/7] Installing Linux-compatible dependencies for Lambda..." -ForegroundColor Yellow

# Clean old lambda_package
if (Test-Path "lambda_package") { Remove-Item lambda_package -Recurse -Force }

# Use Docker python:3.12-slim to install manylinux wheels (required for Lambda Linux runtime)
docker run --rm --platform linux/amd64 -v "${PWD}:/work" -w /work python:3.12-slim pip install --target ./lambda_package -r requirements.txt
if ($LASTEXITCODE -ne 0) {
    Write-Host "  ERROR: Failed to install dependencies via Docker." -ForegroundColor Red
    exit 1
}
Write-Host "  Dependencies installed (Linux manylinux wheels for Python 3.12)." -ForegroundColor Green

# 6. Package and create Lambda function
Write-Host "[6/7] Packaging and creating Lambda function..." -ForegroundColor Yellow

# Create staging directory and copy files
$staging = "deploy\lambda_staging"
if (Test-Path $staging) { Remove-Item $staging -Recurse -Force }
New-Item -ItemType Directory -Path $staging -Force | Out-Null
Copy-Item "lambda_handler.py" $staging\ -Force
Copy-Item "ai_ready" "$staging\ai_ready" -Recurse -Force
Copy-Item "lambda_package" "$staging\lambda_package" -Recurse -Force

# Create zip using Python zipfile (faster and more reliable than Compress-Archive)
Write-Host "  Creating function.zip..." -ForegroundColor DarkGray
if (Test-Path "deploy/function.zip") { Remove-Item "deploy/function.zip" -Force }
python deploy\make_zip.py
Remove-Item $staging -Recurse -Force

# Load env vars from .env for Lambda environment
$envVars = @{}
if (Test-Path ".env") {
    Get-Content ".env" | ForEach-Object {
        if ($_ -match '^\s*([A-Z_]+)\s*=\s*(.+)$') {
            $key = $matches[1]
            $val = $matches[2].Trim()
            if ($key -in @("COCKROACH_DB_URL","GROQ_API_KEY","LLM_PROVIDER","HF_TOKEN")) {
                $envVars[$key] = $val
            }
        }
    }
}
$envVars["S3_KNOWLEDGE_BUCKET"] = "knowledge-base"
$envVars["S3_KNOWLEDGE_PREFIX"] = "docs100"
$envVars["AWS_ENDPOINT_URL"] = "http://floci:4566"

# Build environment JSON manually with proper double quotes (PowerShell ConvertTo-Json doesn't quote properly for AWS CLI)
$varEntries = ($envVars.Keys | ForEach-Object { "`"$_`":`"$($envVars[$_])`"" }) -join ","
$envJson = "{`"Variables`":{$varEntries}}"
# Write to file WITHOUT BOM (AWS CLI rejects BOM in JSON)
[System.IO.File]::WriteAllText((Resolve-Path "deploy").Path + "\lambda-env.json", $envJson, [System.Text.UTF8Encoding]::new($false))

aws lambda create-function `
    --function-name ai-knowledge-assess `
    --runtime python3.12 `
    --handler lambda_handler.lambda_handler `
    --zip-file fileb://deploy/function.zip `
    --role arn:aws:iam::000000000000:role/lambda-role `
    --endpoint-url http://localhost:4566 `
    --timeout 900 `
    --memory-size 2048 `
    --environment file://deploy/lambda-env.json 2>$null

if ($LASTEXITCODE -ne 0) {
    Write-Host "  Function may already exist - updating code + config..." -ForegroundColor DarkYellow
    aws lambda update-function-code `
        --function-name ai-knowledge-assess `
        --zip-file fileb://deploy/function.zip `
        --endpoint-url http://localhost:4566 2>$null
    aws lambda update-function-configuration `
        --function-name ai-knowledge-assess `
        --timeout 900 `
        --memory-size 2048 `
        --environment file://deploy/lambda-env.json `
        --endpoint-url http://localhost:4566 2>$null
}
Write-Host "  Lambda function ready: ai-knowledge-assess (timeout=900s, memory=2048MB)" -ForegroundColor Green

# 7. Set up EventBridge scheduled rule (every 5 min for demo)
Write-Host "[7/7] Setting up EventBridge scheduled rule..." -ForegroundColor Yellow
aws events put-rule `
    --name assess-schedule `
    --schedule-expression "rate(5 minutes)" `
    --endpoint-url http://localhost:4566 2>$null

# Write targets JSON to file (PowerShell strips quotes from inline JSON)
$targetsJson = '[{"Id":"1","Arn":"arn:aws:lambda:us-east-1:000000000000:function:ai-knowledge-assess","Input":"{\"action\":\"assess\"}"}]'
[System.IO.File]::WriteAllText((Resolve-Path "deploy").Path + "\targets.json", $targetsJson, [System.Text.UTF8Encoding]::new($false))

aws events put-targets `
    --rule assess-schedule `
    --targets file://deploy/targets.json `
    --endpoint-url http://localhost:4566 2>$null

# Add Lambda permission for EventBridge
aws lambda add-permission `
    --function-name ai-knowledge-assess `
    --statement-id EventBridgeInvoke `
    --action lambda:InvokeFunction `
    --principal events.amazonaws.com `
    --source-arn arn:aws:events:us-east-1:000000000000:rule/assess-schedule `
    --endpoint-url http://localhost:4566 2>$null

Write-Host "  EventBridge rule: assess-schedule (every 5 minutes)" -ForegroundColor Green

Write-Host ""
Write-Host "=== Deployment Complete ===" -ForegroundColor Green
Write-Host ""
Write-Host "Test commands:"
Write-Host '  aws lambda invoke --function-name ai-knowledge-assess --payload "{\"action\":\"assess\"}" response.json --endpoint-url http://localhost:4566' -ForegroundColor White
Write-Host '  aws lambda invoke --function-name ai-knowledge-assess --payload "{\"action\":\"remediate\"}" response.json --endpoint-url http://localhost:4566' -ForegroundColor White
Write-Host '  aws lambda invoke --function-name ai-knowledge-assess --payload "{\"action\":\"status\"}" response.json --endpoint-url http://localhost:4566' -ForegroundColor White
Write-Host ""
Write-Host "Teardown: .\deploy\teardown.ps1" -ForegroundColor DarkGray
Write-Host 'Cost: $0 - everything runs locally via Docker' -ForegroundColor Green
