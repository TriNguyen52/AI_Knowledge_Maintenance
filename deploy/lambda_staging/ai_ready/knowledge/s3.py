"""
Amazon S3 knowledge connector.

Reads knowledge artifacts from an S3 bucket instead of the local filesystem.
Supports the same file types as the local markdown SDK (.md, .mdx, .txt, .rst)
and reuses the same parser and relation extraction logic.

This satisfies the hackathon requirement to use at least one AWS service.
S3 serves as the artifact/document storage layer — knowledge bases are
stored as versioned objects in S3 and ingested into the assessment pipeline.

Required environment variables:
    AWS_ACCESS_KEY_ID — AWS access key
    AWS_SECRET_ACCESS_KEY — AWS secret key
    AWS_REGION — AWS region (e.g., us-east-1)

Or provide credentials via IAM role when running on AWS infrastructure.
"""

from __future__ import annotations

import io
import os
from pathlib import Path
from typing import Any, Iterator

from ai_ready.knowledge.base import KnowledgeCapability, KnowledgeSDK
from ai_ready.knowledge.markdown_parser import MarkdownDocumentParser
from ai_ready.knowledge.navigation import InlineLinkRelationExtractor
from ai_ready.knowledge.registry import register_knowledge_sdk
from ai_ready.models import KnowledgeArtifact, Relationship


from ai_ready.knowledge.discovery import SUPPORTED_EXTENSIONS


@register_knowledge_sdk
class S3KnowledgeSDK(KnowledgeSDK):
    """S3-backed knowledge SDK for reading knowledge artifacts from Amazon S3.

    Connects to an S3 bucket, lists objects with supported extensions,
    downloads and parses them into KnowledgeArtifact objects using the
    same MarkdownDocumentParser as the local SDK.

    The bucket structure mirrors a local docs directory:
        s3://bucket/docs/intro.md       → artifact uri: docs/intro.md
        s3://bucket/docs/guide/setup.md → artifact uri: docs/guide/setup.md
    """

    name = "s3"
    supported_capabilities = frozenset({
        KnowledgeCapability.ARTIFACTS,
        KnowledgeCapability.RELATIONS,
        KnowledgeCapability.METADATA,
    })

    def __init__(self) -> None:
        super().__init__()
        self._parser = MarkdownDocumentParser()
        self._inline_relation_extractor = InlineLinkRelationExtractor()
        self._bucket: str = ""
        self._prefix: str = ""
        self._s3_client = None
        self._discovered_keys: list[str] = []
        self._artifact_cache: dict[str, KnowledgeArtifact] = {}
        self._relationship_cache: list[Relationship] | None = None

    @classmethod
    def supports(cls, source: str) -> bool:
        """Check if source is an S3 URI (s3://bucket/prefix)."""
        return isinstance(source, str) and source.startswith("s3://")

    def connect(self, source: str | Path) -> None:
        """Connect to an S3 bucket.

        Args:
            source: S3 URI in format 's3://bucket-name/prefix/' or 's3://bucket-name'
        """
        source_str = str(source)
        if not source_str.startswith("s3://"):
            raise ValueError(f"S3KnowledgeSDK requires s3:// URI, got: {source_str}")

        # Parse s3://bucket/prefix
        without_scheme = source_str[5:]  # Remove 's3://'
        parts = without_scheme.split("/", 1)
        self._bucket = parts[0]
        self._prefix = parts[1] if len(parts) > 1 else ""

        # Ensure prefix ends with / if non-empty
        if self._prefix and not self._prefix.endswith("/"):
            self._prefix += "/"

        self._source = Path(source_str)
        self._init_s3_client()
        self._discovered_keys = self._list_objects()
        self._artifact_cache = {}
        self._relationship_cache = None

    def _init_s3_client(self) -> None:
        """Initialize the boto3 S3 client.

        Uses path-style addressing (``http://<endpoint>/<bucket>/<key>``)
        instead of virtual-hosted style (``http://<bucket>.<endpoint>/<key>``).
        Path-style always works — inside Docker containers the virtual-hosted
        wildcard DNS (``*.localhost.floci.io``) may not resolve, causing S3
        requests to hang silently.  When ``AWS_ENDPOINT_URL`` is set (e.g.
        by Floci inside Lambda containers), it is used as the endpoint.
        """
        try:
            import boto3  # type: ignore[import-untyped]
            from botocore.config import Config  # type: ignore[import-untyped]

            region = os.environ.get("AWS_REGION", "us-east-1")
            endpoint_url = os.environ.get("AWS_ENDPOINT_URL")

            kwargs: dict[str, Any] = {
                "region_name": region,
                "config": Config(s3={"addressing_style": "path"}),
            }
            if endpoint_url:
                kwargs["endpoint_url"] = endpoint_url

            self._s3_client = boto3.client("s3", **kwargs)
        except ImportError:
            raise ImportError(
                "boto3 package not installed. Install with: pip install boto3"
            )

    def _list_objects(self) -> list[str]:
        """List all objects in the S3 bucket with supported extensions."""
        keys: list[str] = []
        paginator = self._s3_client.get_paginator("list_objects_v2")

        kwargs: dict[str, Any] = {"Bucket": self._bucket}
        if self._prefix:
            kwargs["Prefix"] = self._prefix

        for page in paginator.paginate(**kwargs):
            for obj in page.get("Contents", []):
                key = obj["Key"]
                ext = os.path.splitext(key)[1].lower()
                if ext in SUPPORTED_EXTENSIONS:
                    keys.append(key)

        return sorted(keys)

    def _download_object(self, key: str) -> str:
        """Download an object from S3 and return its content as text."""
        response = self._s3_client.get_object(Bucket=self._bucket, Key=key)
        body = response["Body"].read()
        return body.decode("utf-8")

    def _key_to_uri(self, key: str) -> str:
        """Convert an S3 key to a knowledge artifact URI."""
        # Strip the prefix to get a relative path
        if self._prefix and key.startswith(self._prefix):
            return key[len(self._prefix):]
        return key

    def iter_artifacts(self) -> Iterator[KnowledgeArtifact]:
        """Yield knowledge artifacts parsed from S3 objects."""
        self._ensure_artifacts()
        for key in self._discovered_keys:
            uri = self._key_to_uri(key)
            artifact = self._artifact_cache.get(uri)
            if artifact is not None:
                yield artifact

    def iter_relationships(self) -> Iterator[Relationship]:
        """Yield relationships extracted from artifact content."""
        self._ensure_relationships()
        yield from self._relationship_cache or []

    def source_metadata(self) -> dict[str, object]:
        return {
            "sdk": self.name,
            "bucket": self._bucket,
            "prefix": self._prefix,
            "file_count": len(self._discovered_keys),
        }

    def _ensure_artifacts(self) -> None:
        """Download and parse all S3 objects into artifacts."""
        if self._artifact_cache:
            return

        for key in self._discovered_keys:
            content = self._download_object(key)
            uri = self._key_to_uri(key)

            # Create a temporary file path for the parser
            # The parser expects a Path, but we already have content
            # So we create the artifact directly from content
            artifact = self._parser.parse_content(
                content=content,
                source_uri=uri,
                source_path=key,
            )
            self._artifact_cache[uri] = artifact

    def _ensure_relationships(self) -> None:
        """Extract inline link relationships from parsed artifacts."""
        if self._relationship_cache is not None:
            return

        self._ensure_artifacts()
        artifacts = list(self._artifact_cache.values())
        self._relationship_cache = list(
            self._inline_relation_extractor.extract(artifacts)
        )
