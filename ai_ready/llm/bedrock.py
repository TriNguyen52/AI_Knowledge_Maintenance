"""
Amazon Bedrock LLM provider implementation.

Uses AWS boto3 to call Amazon Bedrock foundation models:
- Claude 3.5 Sonnet (anthropic.claude-3-5-sonnet-20241022-v2:0) for reasoning
- Titan Text Embeddings (amazon.titan-embed-text-v2:0) for artifact embeddings

This provider integrates AI-Ready's knowledge maintenance workflow with
AWS's managed AI service, satisfying the hackathon requirement to use
at least one AWS service.

Required environment variables:
    AWS_ACCESS_KEY_ID — AWS access key
    AWS_SECRET_ACCESS_KEY — AWS secret key
    AWS_REGION — AWS region (e.g., us-east-1)
"""

from __future__ import annotations

import json
import os
import time
from typing import Any

from ai_ready.llm.base import LLMMessage, LLMProvider, LLMResponse


class BedrockProvider(LLMProvider):
    """Amazon Bedrock-backed LLM provider.

    Calls Bedrock's Converse API for chat completions and InvokeModel
    for embeddings. Supports Claude 3.5 Sonnet, Titan, and other
    Bedrock foundation models.

    Requires boto3 and AWS credentials (environment variables or IAM role).
    """

    name = "bedrock"

    def __init__(
        self,
        default_model: str = "anthropic.claude-3-5-sonnet-20241022-v2:0",
        embedding_model: str = "amazon.titan-embed-text-v2:0",
        region_name: str = "",
        **kwargs: Any,
    ) -> None:
        self._default_model = default_model
        self._embedding_model = embedding_model
        self._region = region_name or os.environ.get("AWS_REGION", "us-east-1")
        self._kwargs = kwargs
        self._client = None
        self._embed_client = None

    def _init_client(self) -> None:
        """Initialize the boto3 Bedrock client lazily."""
        try:
            import boto3  # type: ignore[import-untyped]

            session_kwargs: dict[str, Any] = {"region_name": self._region}
            # Let boto3 handle credential resolution (env vars, IAM role, etc.)
            self._client = boto3.client("bedrock-runtime", **session_kwargs)
        except ImportError:
            raise ImportError(
                "boto3 package not installed. Install with: pip install boto3"
            )

    def _init_embed_client(self) -> None:
        """Initialize the Bedrock client for embeddings."""
        if self._client is None:
            self._init_client()

    @property
    def client(self):
        if self._client is None:
            self._init_client()
        return self._client

    def chat(
        self,
        messages: list[LLMMessage],
        model: str | None = None,
        temperature: float = 0.7,
        max_tokens: int = 4096,
        **kwargs: Any,
    ) -> LLMResponse:
        """Send a chat completion request to Amazon Bedrock.

        Uses the Bedrock Converse API which provides a unified interface
        across all foundation models (Claude, Titan, Llama, Mistral, etc.).
        """
        model_id = model or self._default_model

        # Convert LLMMessage list to Bedrock Converse format
        system_messages = [
            {"text": m.content} for m in messages if m.role == "system"
        ]
        conversation_messages = []
        for m in messages:
            if m.role == "system":
                continue
            conversation_messages.append({
                "role": m.role if m.role in ("user", "assistant") else "user",
                "content": [{"text": m.content}],
            })

        converse_params: dict[str, Any] = {
            "modelId": model_id,
            "messages": conversation_messages,
            "inferenceConfig": {
                "temperature": temperature,
                "maxTokens": max_tokens,
            },
        }
        if system_messages:
            converse_params["system"] = system_messages

        # Merge any extra kwargs into inferenceConfig
        for key in ("top_p", "stop_sequences"):
            if key in kwargs:
                converse_params["inferenceConfig"][key] = kwargs.pop(key)

        start = time.monotonic()
        response = self.client.converse(**converse_params)
        latency_ms = (time.monotonic() - start) * 1000

        # Extract response content
        output = response.get("output", {})
        message = output.get("message", {})
        content_blocks = message.get("content", [])
        content_text = "".join(
            block.get("text", "") for block in content_blocks if "text" in block
        )

        # Extract usage
        usage = response.get("usage", {})

        return LLMResponse(
            content=content_text,
            model=model_id,
            provider=self.name,
            prompt_tokens=usage.get("inputTokens", 0),
            completion_tokens=usage.get("outputTokens", 0),
            latency_ms=latency_ms,
            raw=response,
        )

    def stream_chat(
        self,
        messages: list[LLMMessage],
        model: str | None = None,
        temperature: float = 0.7,
        max_tokens: int = 4096,
        **kwargs: Any,
    ):
        """Stream a chat completion from Amazon Bedrock."""
        model_id = model or self._default_model

        system_messages = [
            {"text": m.content} for m in messages if m.role == "system"
        ]
        conversation_messages = []
        for m in messages:
            if m.role == "system":
                continue
            conversation_messages.append({
                "role": m.role if m.role in ("user", "assistant") else "user",
                "content": [{"text": m.content}],
            })

        converse_params: dict[str, Any] = {
            "modelId": model_id,
            "messages": conversation_messages,
            "inferenceConfig": {
                "temperature": temperature,
                "maxTokens": max_tokens,
            },
        }
        if system_messages:
            converse_params["system"] = system_messages

        response = self.client.converse_stream(**converse_params)
        for event in response.get("stream", []):
            if "chunk" in event:
                chunk = event["chunk"]
                if "bytes" in chunk:
                    import io
                    data = json.loads(io.BytesIO(chunk["bytes"]).read().decode("utf-8"))
                    if "delta" in data and "text" in data["delta"]:
                        yield data["delta"]["text"]

    def embed_text(self, text: str, model: str | None = None) -> list[float]:
        """Generate embeddings for a text using Amazon Bedrock Titan.

        Uses amazon.titan-embed-text-v2:0 by default, which produces
        1536-dimensional embeddings compatible with CockroachDB's
        VECTOR(1536) type.
        """
        model_id = model or self._embedding_model

        if self._client is None:
            self._init_client()

        response = self.client.invoke_model(
            modelId=model_id,
            body=json.dumps({
                "inputText": text,
                "dimensions": 1536,
                "normalize": True,
            }),
            accept="application/json",
            contentType="application/json",
        )

        body = json.loads(response["body"].read())
        return body.get("embedding", [])

    def embed_artifact(
        self, artifact_uri: str, content: str, metadata: dict[str, Any] | None = None
    ) -> list[float]:
        """Generate an embedding for a knowledge artifact.

        Combines the artifact URI, content, and key metadata into a single
        embedding that captures the artifact's semantic meaning for
        similarity search via CockroachDB's distributed vector indexing.
        """
        # Build a text representation that captures the artifact's meaning
        meta_str = ""
        if metadata:
            key_fields = ["title", "description", "tags", "category"]
            meta_parts = [
                f"{k}: {metadata[k]}" for k in key_fields if k in metadata
            ]
            if meta_parts:
                meta_str = " | " + " | ".join(meta_parts)

        embed_text = f"{artifact_uri}{meta_str}\n\n{content[:8000]}"
        return self.embed_text(embed_text)

    @property
    def default_model(self) -> str:
        return self._default_model
