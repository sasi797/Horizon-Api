import asyncio
from functools import partial

import boto3

from app.config import settings


def _client():
    return boto3.client(
        "s3",
        region_name=settings.aws_region,
        aws_access_key_id=settings.aws_access_key_id or None,
        aws_secret_access_key=settings.aws_secret_access_key or None,
    )


def s3_key(booking_id: str, message_id: str, filename: str) -> str:
    return f"{settings.s3_prefix}/{booking_id}/{message_id}/{filename}"


def upload_bytes_sync(data: bytes, key: str, content_type: str = "application/octet-stream") -> str:
    _client().put_object(
        Bucket=settings.s3_bucket,
        Key=key,
        Body=data,
        ContentType=content_type,
    )
    return key


async def upload_bytes(data: bytes, key: str, content_type: str = "application/octet-stream") -> str:
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, partial(upload_bytes_sync, data, key, content_type))


def presigned_url_sync(key: str, expires: int = 3600) -> str:
    return _client().generate_presigned_url(
        "get_object",
        Params={"Bucket": settings.s3_bucket, "Key": key},
        ExpiresIn=expires,
    )


async def presigned_url(key: str, expires: int = 3600) -> str:
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, partial(presigned_url_sync, key, expires))
