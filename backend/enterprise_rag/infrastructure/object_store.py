"""对象存储实现：提供本地文件系统和 S3 兼容存储两种后端。"""

from __future__ import annotations

import asyncio
from pathlib import Path, PurePosixPath
from typing import Any, cast

import boto3  # type: ignore[import-untyped]
from botocore.config import Config  # type: ignore[import-untyped]
from botocore.exceptions import ClientError  # type: ignore[import-untyped]


def tenant_object_key(tenant_id: str, category: str, object_id: str, name: str) -> str:
    """生成带租户隔离路径的对象键，并只保留文件名部分。"""
    safe_name = Path(name).name
    path = PurePosixPath("tenants", tenant_id, category, object_id, safe_name)
    if ".." in path.parts:
        raise ValueError("invalid object key")
    return str(path)


class FileSystemObjectStore:
    """本地对象存储：限制所有路径都位于配置的根目录内。"""

    def __init__(self, root: Path) -> None:
        """解析并保存对象根目录。"""
        self.root = root.resolve()

    def _resolve(self, key: str) -> Path:
        """解析对象键并阻止路径穿越存储根目录。"""
        candidate = (self.root / Path(key)).resolve()
        if self.root not in candidate.parents and candidate != self.root:
            raise ValueError("object key escapes storage root")
        return candidate

    async def put(self, key: str, data: bytes, content_type: str) -> str:
        """创建父目录并异步写入对象内容。"""
        del content_type
        path = self._resolve(key)
        await asyncio.to_thread(path.parent.mkdir, parents=True, exist_ok=True)
        await asyncio.to_thread(path.write_bytes, data)
        return key

    async def get(self, key: str) -> bytes:
        """读取对象；对象不存在时抛出 FileNotFoundError。"""
        path = self._resolve(key)
        if not await asyncio.to_thread(path.exists):
            raise FileNotFoundError(key)
        return await asyncio.to_thread(path.read_bytes)

    async def delete(self, key: str) -> None:
        """删除存在的本地对象。"""
        path = self._resolve(key)
        if await asyncio.to_thread(path.exists):
            await asyncio.to_thread(path.unlink)

    async def exists(self, key: str) -> bool:
        """检查对象是否存在且路径仍在存储根目录内。"""
        return await asyncio.to_thread(self._resolve(key).exists)

    async def health(self) -> bool:
        """通过检查根目录判断本地存储是否就绪。"""
        return await asyncio.to_thread(self.root.exists)


class S3ObjectStore:
    """S3 兼容对象存储，支持 MinIO 所需的 path-style 访问。"""

    def __init__(
        self,
        endpoint_url: str,
        bucket: str,
        access_key_id: str,
        secret_access_key: str,
        region: str = "us-east-1",
    ) -> None:
        """创建 boto3 客户端并保存目标 bucket。"""
        self.bucket = bucket
        self._client: Any = boto3.client(
            "s3",
            endpoint_url=endpoint_url,
            aws_access_key_id=access_key_id,
            aws_secret_access_key=secret_access_key,
            region_name=region,
            config=Config(signature_version="s3v4", s3={"addressing_style": "path"}),
        )

    async def start(self) -> None:
        """确认 bucket 存在，不存在时创建 bucket。"""
        try:
            await asyncio.to_thread(self._client.head_bucket, Bucket=self.bucket)
        except ClientError as exc:
            code = str(exc.response.get("Error", {}).get("Code", ""))
            if code not in {"404", "NoSuchBucket", "NotFound"}:
                raise
            await asyncio.to_thread(self._client.create_bucket, Bucket=self.bucket)

    async def put(self, key: str, data: bytes, content_type: str) -> str:
        """把内容和 MIME 类型上传到 S3 对象。"""
        await asyncio.to_thread(
            self._client.put_object,
            Bucket=self.bucket,
            Key=key,
            Body=data,
            ContentType=content_type,
        )
        return key

    async def get(self, key: str) -> bytes:
        """读取 S3 对象，并把不存在映射为 FileNotFoundError。"""
        try:
            response = await asyncio.to_thread(
                self._client.get_object, Bucket=self.bucket, Key=key
            )
        except ClientError as exc:
            code = str(exc.response.get("Error", {}).get("Code", ""))
            if code in {"404", "NoSuchKey", "NotFound"}:
                raise FileNotFoundError(key) from exc
            raise
        return cast(bytes, await asyncio.to_thread(response["Body"].read))

    async def delete(self, key: str) -> None:
        """删除 S3 对象。"""
        await asyncio.to_thread(self._client.delete_object, Bucket=self.bucket, Key=key)

    async def exists(self, key: str) -> bool:
        """通过 head_object 检查对象是否存在。"""
        try:
            await asyncio.to_thread(self._client.head_object, Bucket=self.bucket, Key=key)
            return True
        except ClientError as exc:
            code = str(exc.response.get("Error", {}).get("Code", ""))
            if code in {"404", "NoSuchKey", "NotFound"}:
                return False
            raise

    async def health(self) -> bool:
        """通过 head_bucket 检查 S3 bucket 是否可访问。"""
        try:
            await asyncio.to_thread(self._client.head_bucket, Bucket=self.bucket)
            return True
        except Exception:
            return False
