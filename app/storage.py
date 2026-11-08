"""Backblaze B2 private bucket accessed through its S3-compatible API."""
import json
from datetime import datetime, timezone
from typing import Any

import boto3
from botocore.config import Config as BotoConfig
from botocore.exceptions import ClientError
from boto3.s3.transfer import TransferConfig


class MissingObject(KeyError):
    pass


class Storage:
    def __init__(self, settings):
        if not settings.storage_ready:
            raise RuntimeError("Backblaze environment variables are missing")
        self.bucket = settings.bucket
        region = settings.endpoint.removeprefix("https://s3.").split(".", 1)[0]
        self.s3 = boto3.client(
            "s3", endpoint_url=settings.endpoint,
            aws_access_key_id=settings.key_id,
            aws_secret_access_key=settings.secret,
            region_name=region,
            config=BotoConfig(signature_version="s3v4", connect_timeout=10, read_timeout=60,
                              retries={"max_attempts": 3, "mode": "standard"},
                              s3={"addressing_style": "path"}),
        )
        self.transfer = TransferConfig(multipart_threshold=16 * 1024 * 1024,
                                       multipart_chunksize=8 * 1024 * 1024,
                                       max_concurrency=1, use_threads=False)

    def _call(self, method: str, **kwargs):
        try:
            return getattr(self.s3, method)(Bucket=self.bucket, **kwargs)
        except ClientError as exc:
            status = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
            error_code = exc.response.get("Error", {}).get("Code")
            if status == 404 or error_code in ("NoSuchKey", "NotFound", "404"):
                raise MissingObject(kwargs.get("Key", "")) from exc
            raise

    def head(self, key: str) -> dict[str, Any] | None:
        try:
            return self._call("head_object", Key=key)
        except MissingObject:
            return None

    def get_bytes(self, key: str, max_bytes: int = 4 * 1024 * 1024) -> bytes:
        result = self._call("get_object", Key=key)
        body = result["Body"]
        try:
            data = body.read(max_bytes + 1)
        finally:
            body.close()
        if len(data) > max_bytes:
            raise ValueError("Storage object exceeds allowed size")
        return data

    def get_json(self, key: str):
        return json.loads(self.get_bytes(key).decode("utf-8"))

    def put_bytes(self, key: str, data: bytes, content_type: str) -> None:
        self._call("put_object", Key=key, Body=data, ContentType=content_type,
                   CacheControl="private, max-age=0")

    def put_json(self, key: str, data: Any) -> None:
        encoded = json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.put_bytes(key, encoded, "application/json")

    def upload_file(self, key: str, path: str, content_type: str = "audio/mpeg") -> None:
        self.s3.upload_file(path, self.bucket, key,
                            ExtraArgs={"ContentType": content_type}, Config=self.transfer)

    def download_file(self, key: str, path: str) -> None:
        self.s3.download_file(self.bucket, key, path, Config=self.transfer)

    def stream(self, key: str, byte_range: str | None = None):
        """Yield chunks in a worker thread; close the S3 body on client disconnect."""
        args = {"Key": key}
        if byte_range:
            args["Range"] = byte_range
        response = self._call("get_object", **args)
        body = response["Body"]
        try:
            for chunk in body.iter_chunks(chunk_size=128 * 1024):
                if chunk:
                    yield chunk
        finally:
            body.close()

    def list_current(self, prefix: str) -> list[dict]:
        items: list[dict] = []
        paginator = self.s3.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=self.bucket, Prefix=prefix):
            items.extend(page.get("Contents", []))
        return items

    def delete_versions(self, key: str) -> None:
        """Permanently delete all versions, not just add an S3 delete marker."""
        paginator = self.s3.get_paginator("list_object_versions")
        for page in paginator.paginate(Bucket=self.bucket, Prefix=key):
            versions = page.get("Versions", []) + page.get("DeleteMarkers", [])
            for version in versions:
                if version["Key"] == key:
                    self._call("delete_object", Key=key, VersionId=version["VersionId"])

    def expire_cache(self, prefix: str, older_than_days: int, budget: int, reserve: int = 0) -> None:
        """Delete expired items and oldest cached audio to maintain an upper bound."""
        entries = sorted(self.list_current(prefix), key=lambda x: x["LastModified"])
        now = datetime.now(timezone.utc)
        kept = []
        for item in entries:
            if (now - item["LastModified"]).total_seconds() > older_than_days * 86400:
                self.delete_versions(item["Key"])
            else:
                kept.append(item)
        used = sum(item["Size"] for item in kept)
        for item in kept:
            if used + reserve <= budget:
                break
            self.delete_versions(item["Key"])
            used -= item["Size"]
        if used + reserve > budget:
            raise ValueError("Cache capacity reached: this episode is too large")
