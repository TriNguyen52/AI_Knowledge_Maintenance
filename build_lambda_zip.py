"""Build Lambda function zip package for Floci deployment."""
import zipfile
import os

# Remove old zip
if os.path.exists('deploy/function.zip'):
    os.remove('deploy/function.zip')

with zipfile.ZipFile('deploy/function.zip', 'w', zipfile.ZIP_DEFLATED) as z:
    # Add lambda_handler.py at root
    z.write('ai_ready/cloud/lambda_handler.py', 'lambda_handler.py')

    # Add ai_ready package
    for root, dirs, files in os.walk('ai_ready'):
        dirs[:] = [d for d in dirs if d != '__pycache__']
        for f in files:
            if f.endswith('.pyc'):
                continue
            filepath = os.path.join(root, f)
            z.write(filepath, filepath)

    # Add lambda_package dependencies
    for root, dirs, files in os.walk('lambda_package'):
        dirs[:] = [d for d in dirs if d not in ('__pycache__',)]
        for f in files:
            if f.endswith('.pyc'):
                continue
            filepath = os.path.join(root, f)
            arcname = os.path.relpath(filepath, 'lambda_package')
            z.write(filepath, arcname)

size = os.path.getsize('deploy/function.zip')
print(f"Zip created: {size} bytes")

# Verify
with zipfile.ZipFile('deploy/function.zip') as z:
    has_handler = 'lambda_handler.py' in z.namelist()
    print(f"lambda_handler.py at root: {has_handler}")
    total_files = len(z.namelist())
    print(f"Total files in zip: {total_files}")
