"""
Garage Client - S3-compatible object storage client for generated audio.

Garage (https://garagehq.deuxfleurs.fr/) is an S3-compatible distributed object
storage designed for self-hosting. This client wraps boto3 to provide async
operations for the worker process.

Usage:
    config = GarageConfig(
        endpoint="http://garage:3900",
        access_key=os.environ["GARAGE_ACCESS_KEY"],
        secret_key=os.environ["GARAGE_SECRET_KEY"],
        bucket="mcclanker",
        region="garage"
    )
    client = GarageClient(config)
    await client.put_object("audio/job-123.aac", audio_bytes)
    audio_bytes = await client.get_object("audio/job-123.aac")
    url = client.get_presigned_url("audio/job-123.aac")  # For streaming
"""

import asyncio
import os
from dataclasses import dataclass

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError


@dataclass
class GarageConfig:
    """Configuration for Garage S3-compatible storage."""
    endpoint: str
    access_key: str
    secret_key: str
    bucket: str
    region: str = "garage"


class GarageClient:
    """
    Async wrapper around boto3 S3 client for Garage object storage.

    All blocking S3 operations are run in a thread pool executor to avoid
    blocking the async event loop.
    """

    def __init__(self, config: GarageConfig):
        self.bucket = config.bucket
        self.config = config
        self._client = boto3.client(
            's3',
            endpoint_url=config.endpoint,
            aws_access_key_id=config.access_key,
            aws_secret_access_key=config.secret_key,
            region_name=config.region,
            config=Config(signature_version='s3v4'),
        )

    def _sync_put_object(self, key: str, data: bytes) -> None:
        """
        Synchronous put - runs in thread pool executor.

        Args:
            key: Object key in bucket (e.g., "audio/job-123.aac")
            data: Raw bytes to upload
        """
        self._client.put_object(
            Bucket=self.bucket,
            Key=key,
            Body=data
        )

    def _sync_get_object(self, key: str) -> bytes:
        """
        Synchronous get - runs in thread pool executor.

        Args:
            key: Object key in bucket

        Returns:
            Raw bytes from object
        """
        response = self._client.get_object(Bucket=self.bucket, Key=key)
        return response['Body'].read()

    def _sync_delete_object(self, key: str) -> None:
        """
        Synchronous delete - runs in thread pool executor.

        Args:
            key: Object key in bucket
        """
        self._client.delete_object(Bucket=self.bucket, Key=key)

    async def put_object(self, key: str, data: bytes) -> None:
        """
        Upload bytes to Garage asynchronously.

        Args:
            key: Object key in bucket (e.g., "audio/job-123.aac")
            data: Raw bytes to upload
        """
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, self._sync_put_object, key, data)

    async def get_object(self, key: str) -> bytes:
        """
        Download bytes from Garage asynchronously.

        Args:
            key: Object key in bucket

        Returns:
            Raw bytes from object
        """
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, self._sync_get_object, key)

    async def delete_object(self, key: str) -> None:
        """
        Delete an object from Garage asynchronously.

        Args:
            key: Object key in bucket
        """
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, self._sync_delete_object, key)

    def get_presigned_url(self, key: str, expires_in: int = 3600) -> str:
        """
        Generate a presigned URL for temporary direct access.

        Presigned URLs allow temporary access to private objects without
        requiring authentication credentials.

        Args:
            key: Object key in bucket
            expires_in: Seconds until URL expires (default 1 hour)

        Returns:
            Presigned URL string that can be used to GET the object
        """
        return self._client.generate_presigned_url(
            'get_object',
            Params={'Bucket': self.bucket, 'Key': key},
            ExpiresIn=expires_in
        )

    async def exists(self, key: str) -> bool:
        """
        Check if an object exists in Garage.

        Args:
            key: Object key in bucket

        Returns:
            True if object exists, False otherwise
        """
        loop = asyncio.get_event_loop()
        try:
            await loop.run_in_executor(
                None,
                lambda: self._client.head_object(Bucket=self.bucket, Key=key)
            )
            return True
        except ClientError:
            return False


def create_garage_client_from_env() -> "GarageClient":
    """Create GarageClient from environment variables."""
    config = GarageConfig(
        endpoint=os.environ["GARAGE_ENDPOINT"],
        access_key=os.environ["GARAGE_ACCESS_KEY"],
        secret_key=os.environ["GARAGE_SECRET_KEY"],
        bucket=os.environ["GARAGE_BUCKET"],
        region=os.environ.get("GARAGE_BUCKET_REGION", "garage"),
    )
    return GarageClient(config)


__all__ = ["GarageClient", "GarageConfig", "create_garage_client_from_env"]
