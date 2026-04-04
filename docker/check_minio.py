#!/usr/bin/env python3
import boto3
import os

endpoint = os.environ.get('GARAGE_ENDPOINT', 'http://minio:9000')
access_key = os.environ.get('GARAGE_ACCESS_KEY', '')
secret_key = os.environ.get('GARAGE_SECRET_KEY', '')
bucket = os.environ.get('GARAGE_BUCKET', 'mcclanker')

print(f"Checking bucket '{bucket}' at {endpoint}")

from botocore.config import Config
client = boto3.client('s3', endpoint_url=endpoint, aws_access_key_id=access_key, aws_secret_access_key=secret_key, region_name='garage', config=Config(signature_version='s3v4'))

try:
    result = client.list_objects_v2(Bucket=bucket, Prefix='audio/', MaxKeys=10)
    contents = result.get('Contents', [])
    print(f'Found {len(contents)} objects')
    for obj in contents:
        print(f"  {obj['Key']} - {obj.get('Size', '?')} bytes")
except Exception as e:
    print(f'Error: {e}')