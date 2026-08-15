"""Create function.zip for Lambda deployment.

Packages lambda_handler.py + ai_ready/ + lambda_package/ into a single
zip file at deploy/function.zip.
"""
import os
import zipfile

staging = "deploy/lambda_staging"
output = "deploy/function.zip"

if not os.path.isdir(staging):
    raise SystemExit(f"Staging directory not found: {staging}")

if os.path.exists(output):
    os.remove(output)

count = 0
with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as zf:
    for root, dirs, files in os.walk(staging):
        for f in files:
            full = os.path.join(root, f)
            arc = os.path.relpath(full, staging)
            zf.write(full, arc)
            count += 1

print(f"Created {output} with {count} files")
