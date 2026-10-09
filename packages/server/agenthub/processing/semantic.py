"""Pinned backend embeddings; PostgreSQL owns vector storage and retrieval."""
from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import tempfile
import threading
from typing import Protocol
from urllib.parse import quote
from urllib.request import Request, urlopen


MODEL_ID = "nomic-ai/nomic-embed-text-v1.5"
MODEL_REVISION = "e5cf08aadaa33385f5990def41f7a23405aec398"
MODEL_SHA256 = "cf5b5a86edb00f895561803cfc04729090a958340b8ca2ad76c143f565f6bb04"
MODEL_FILENAME = "onnx/model_fp16.onnx"
TOKENIZER_FILENAME = "tokenizer.json"
DIMENSION = 512
NATIVE_DIMENSION = 768
MAX_TOKENS = 512
MANIFEST_SCHEMA_VERSION = 1


class SemanticError(RuntimeError):
    """Base class with a stable, content-free error code."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


class SemanticUnavailable(SemanticError):
    """The caller should continue with lexical retrieval."""


class SemanticSetupError(SemanticError):
    """Explicit setup failed without making a partial model available."""


@dataclass(frozen=True)
class ModelAsset:
    path: str
    sha256: str | None
    maximum_bytes: int


@dataclass(frozen=True)
class ModelSpec:
    model_id: str
    revision: str
    dimension: int
    native_dimension: int
    model_file: str
    tokenizer_file: str
    assets: tuple[ModelAsset, ...]

    @property
    def key(self) -> str:
        return f"{self.model_id}@{self.revision}:fp16:matryoshka-{self.dimension}"


PINNED_MODEL = ModelSpec(
    model_id=MODEL_ID,
    revision=MODEL_REVISION,
    dimension=DIMENSION,
    native_dimension=NATIVE_DIMENSION,
    model_file=MODEL_FILENAME,
    tokenizer_file=TOKENIZER_FILENAME,
    assets=(
        ModelAsset(MODEL_FILENAME, MODEL_SHA256, 400_000_000),
        ModelAsset(TOKENIZER_FILENAME, None, 5_000_000),
    ),
)
MODEL_KEY = PINNED_MODEL.key


class Embedder(Protocol):
    @property
    def model_key(self) -> str: ...

    @property
    def dimension(self) -> int: ...

    def embed_documents(self, texts: list[str]) -> list[list[float]]: ...

    def embed_queries(self, texts: list[str]) -> list[list[float]]: ...


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _safe_asset_path(root: Path, relative: str) -> Path:
    candidate = root.joinpath(*Path(relative).parts)
    if Path(relative).is_absolute() or ".." in Path(relative).parts:
        raise SemanticSetupError("semantic_asset_path_invalid")
    return candidate


def model_directory(home: str | os.PathLike[str], spec: ModelSpec = PINNED_MODEL) -> Path:
    slug = spec.model_id.rsplit("/", 1)[-1]
    return Path(home) / "models" / slug / spec.revision


def _private(path: Path) -> bool:
    return os.name != "posix" or not (path.stat().st_mode & 0o077)


def _manifest_payload(spec: ModelSpec, hashes: Mapping[str, str]) -> dict:
    return {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "model_id": spec.model_id,
        "revision": spec.revision,
        "model_key": spec.key,
        "dimension": spec.dimension,
        "native_dimension": spec.native_dimension,
        "model_file": spec.model_file,
        "tokenizer_file": spec.tokenizer_file,
        "files": dict(sorted(hashes.items())),
    }


def verify_model_directory(directory: str | os.PathLike[str],
                           spec: ModelSpec = PINNED_MODEL) -> dict:
    """Verify a complete private cache without modifying it or using a network."""
    root = Path(directory)
    if not root.is_dir() or root.is_symlink():
        raise SemanticUnavailable("semantic_model_not_installed")
    if not _private(root):
        raise SemanticUnavailable("semantic_cache_not_private")
    manifest_path = root / "manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError) as exc:
        raise SemanticUnavailable("semantic_manifest_invalid") from exc
    expected = {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "model_id": spec.model_id,
        "revision": spec.revision,
        "model_key": spec.key,
        "dimension": spec.dimension,
        "native_dimension": spec.native_dimension,
        "model_file": spec.model_file,
        "tokenizer_file": spec.tokenizer_file,
    }
    if any(manifest.get(key) != value for key, value in expected.items()):
        raise SemanticUnavailable("semantic_manifest_mismatch")
    recorded = manifest.get("files")
    if not isinstance(recorded, dict) or set(recorded) != {asset.path for asset in spec.assets}:
        raise SemanticUnavailable("semantic_manifest_invalid")
    for asset in spec.assets:
        path = _safe_asset_path(root, asset.path)
        if not path.is_file() or path.is_symlink() or not _private(path):
            raise SemanticUnavailable("semantic_model_file_invalid")
        digest = _sha256(path)
        if recorded.get(asset.path) != digest or (asset.sha256 and digest != asset.sha256):
            raise SemanticUnavailable("semantic_model_checksum_mismatch")
    return manifest


def _download(url: str, destination: Path, maximum_bytes: int) -> None:
    request = Request(url, headers={"User-Agent": "vaelius-client/0.1"})
    try:
        response = urlopen(request, timeout=60)
        final_url = response.geturl()
        if not final_url.startswith("https://"):
            raise SemanticSetupError("semantic_download_insecure_redirect")
        total = 0
        with response, destination.open("xb") as target:
            while block := response.read(1024 * 1024):
                total += len(block)
                if total > maximum_bytes:
                    raise SemanticSetupError("semantic_download_too_large")
                target.write(block)
    except SemanticError:
        raise
    except (OSError, ValueError) as exc:
        raise SemanticSetupError("semantic_download_failed") from exc


def setup_model(
    home: str | os.PathLike[str],
    *,
    spec: ModelSpec = PINNED_MODEL,
    downloader: Callable[[str, Path, int], None] | None = None,
) -> dict:
    """Explicitly install the pinned model; ordinary retrieval never calls this."""
    target = model_directory(home, spec)
    if target.exists():
        manifest = verify_model_directory(target, spec)
        return {"installed": False, "model_key": spec.key, "directory": str(target),
                "files": sorted(manifest["files"])}
    parent = target.parent
    parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if os.name == "posix":
        os.chmod(parent, 0o700)
    temporary = Path(tempfile.mkdtemp(prefix="semantic-setup-", dir=parent))
    fetch = downloader or _download
    try:
        hashes = {}
        for asset in spec.assets:
            destination = _safe_asset_path(temporary, asset.path)
            destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            if os.name == "posix":
                os.chmod(destination.parent, 0o700)
            escaped = "/".join(quote(part, safe="") for part in Path(asset.path).parts)
            url = f"https://huggingface.co/{spec.model_id}/resolve/{spec.revision}/{escaped}"
            fetch(url, destination, asset.maximum_bytes)
            if not destination.is_file() or destination.is_symlink():
                raise SemanticSetupError("semantic_download_missing_file")
            if destination.stat().st_size > asset.maximum_bytes:
                raise SemanticSetupError("semantic_download_too_large")
            if os.name == "posix":
                os.chmod(destination, 0o600)
            digest = _sha256(destination)
            if asset.sha256 and digest != asset.sha256:
                raise SemanticSetupError("semantic_model_checksum_mismatch")
            hashes[asset.path] = digest
        manifest_path = temporary / "manifest.json"
        manifest_path.write_text(
            json.dumps(_manifest_payload(spec, hashes), sort_keys=True, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )
        if os.name == "posix":
            os.chmod(manifest_path, 0o600)
            os.chmod(temporary, 0o700)
        try:
            os.rename(temporary, target)
        except FileExistsError:
            verify_model_directory(target, spec)
        manifest = verify_model_directory(target, spec)
        return {"installed": True, "model_key": spec.key, "directory": str(target),
                "files": sorted(manifest["files"])}
    except SemanticError:
        raise
    except OSError as exc:
        raise SemanticSetupError("semantic_setup_failed") from exc
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)


def prepare_document(text: str) -> str:
    if not isinstance(text, str) or not text.strip():
        raise ValueError("semantic_text_required")
    return "search_document: " + text.strip()


def prepare_query(text: str) -> str:
    if not isinstance(text, str) or not text.strip():
        raise ValueError("semantic_text_required")
    return "search_query: " + text.strip()


def normalize_vector(values: Sequence[float], dimension: int | None = None) -> list[float]:
    result = [float(value) for value in values]
    if dimension is not None and len(result) != dimension:
        raise ValueError("semantic_dimension_mismatch")
    if not result or not all(math.isfinite(value) for value in result):
        raise ValueError("semantic_vector_invalid")
    magnitude = math.sqrt(sum(value * value for value in result))
    if not math.isfinite(magnitude) or magnitude <= 0:
        raise ValueError("semantic_zero_vector")
    return [value / magnitude for value in result]


def _nomic_postprocess(values: Sequence[float], dimension: int = DIMENSION) -> list[float]:
    raw = [float(value) for value in values]
    if len(raw) < dimension or not all(math.isfinite(value) for value in raw):
        raise SemanticUnavailable("semantic_model_output_invalid")
    mean = sum(raw) / len(raw)
    variance = sum((value - mean) ** 2 for value in raw) / len(raw)
    denominator = math.sqrt(variance + 1e-5)
    layered = [(value - mean) / denominator for value in raw]
    return normalize_vector(layered[:dimension], dimension)








class _OnnxRuntime:
    def __init__(self, directory: Path, spec: ModelSpec):
        try:
            import numpy as np
            import onnxruntime as ort
            from tokenizers import Tokenizer
        except ImportError as exc:
            raise SemanticUnavailable("semantic_dependencies_missing") from exc
        self.np = np
        try:
            self.tokenizer = Tokenizer.from_file(str(directory / spec.tokenizer_file))
            self.tokenizer.enable_truncation(max_length=MAX_TOKENS)
            available = ort.get_available_providers()
            providers = (["CoreMLExecutionProvider", "CPUExecutionProvider"]
                         if "CoreMLExecutionProvider" in available else ["CPUExecutionProvider"])
            options=ort.SessionOptions();options.log_severity_level=3
            self.session = ort.InferenceSession(str(directory / spec.model_file),
                                                sess_options=options,providers=providers)
        except Exception as exc:
            raise SemanticUnavailable("semantic_runtime_load_failed") from exc
        self.dimension = spec.dimension

    def encode(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        try:
            encoded = self.tokenizer.encode_batch(texts)
            maximum = max(len(item.ids) for item in encoded)
            pad = self.tokenizer.token_to_id("[PAD]") or 0
            ids = self.np.full((len(encoded), maximum), pad, dtype=self.np.int64)
            mask = self.np.zeros((len(encoded), maximum), dtype=self.np.int64)
            types = self.np.zeros((len(encoded), maximum), dtype=self.np.int64)
            for index, item in enumerate(encoded):
                length = len(item.ids)
                ids[index, :length] = item.ids
                mask[index, :length] = item.attention_mask
                if item.type_ids:
                    types[index, :length] = item.type_ids
            inputs = {item.name for item in self.session.get_inputs()}
            feed = {"input_ids": ids}
            if "attention_mask" in inputs:
                feed["attention_mask"] = mask
            if "token_type_ids" in inputs:
                feed["token_type_ids"] = types
            unsupported = inputs - set(feed)
            if unsupported:
                raise SemanticUnavailable("semantic_model_input_unsupported")
            outputs = self.session.run(None, feed)
            hidden = next((value for value in outputs if getattr(value, "ndim", 0) == 3), None)
            if hidden is None:
                hidden = next((value for value in outputs if getattr(value, "ndim", 0) == 2), None)
            if hidden is None:
                raise SemanticUnavailable("semantic_model_output_invalid")
            if hidden.ndim == 3:
                weighted = hidden * mask[:, :, None]
                pooled = weighted.sum(axis=1) / self.np.maximum(mask.sum(axis=1, keepdims=True), 1)
            else:
                pooled = hidden
            return [_nomic_postprocess(row.tolist(), self.dimension) for row in pooled]
        except SemanticError:
            raise
        except Exception as exc:
            raise SemanticUnavailable("semantic_inference_failed") from exc


class SemanticModel:
    """Lazy offline model facade used by retrieval and backfill code."""

    def __init__(self, directory: Path, spec: ModelSpec = PINNED_MODEL, *, verified: bool = False):
        self.directory = directory
        self.spec = spec
        self._verified = verified
        self._runtime: _OnnxRuntime | None = None
        self._lock = threading.RLock()

    @classmethod
    def from_home(cls, home: str | os.PathLike[str], require_files: bool = True, *,
                  spec: ModelSpec = PINNED_MODEL) -> "SemanticModel":
        directory = model_directory(home, spec)
        if require_files:
            verify_model_directory(directory, spec)
        return cls(directory, spec, verified=require_files)

    @property
    def model_key(self) -> str:
        return self.spec.key

    @property
    def dimension(self) -> int:
        return self.spec.dimension

    def _get_runtime(self) -> _OnnxRuntime:
        if not self._verified:
            verify_model_directory(self.directory, self.spec)
            self._verified = True
        if self._runtime is None:
            self._runtime = _OnnxRuntime(self.directory, self.spec)
        return self._runtime

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        prepared = [prepare_document(text) for text in texts]
        with self._lock:return self._get_runtime().encode(prepared) if prepared else []

    def embed_queries(self, texts: list[str]) -> list[list[float]]:
        prepared = [prepare_query(text) for text in texts]
        with self._lock:return self._get_runtime().encode(prepared) if prepared else []










def check_conformance(model: Embedder) -> dict:
    """Check the backend contract without exposing vector or source contents."""
    documents = model.embed_documents(["A service should retry transient failures with backoff."])
    queries = model.embed_queries(["How should temporary failures be retried?"])
    repeated = model.embed_queries(["How should temporary failures be retried?"])
    vectors = documents + queries + repeated
    if len(documents) != 1 or len(queries) != 1 or len(repeated) != 1:
        raise SemanticUnavailable("semantic_conformance_count_failed")
    if any(len(vector) != model.dimension for vector in vectors):
        raise SemanticUnavailable("semantic_conformance_dimension_failed")
    if any(abs(math.sqrt(sum(value * value for value in vector)) - 1.0) > 1e-5
           for vector in vectors):
        raise SemanticUnavailable("semantic_conformance_normalization_failed")
    if any(abs(left - right) > 1e-6 for left, right in zip(queries[0], repeated[0])):
        raise SemanticUnavailable("semantic_conformance_determinism_failed")
    return {"model_key": model.model_key, "dimension": model.dimension,
            "normalized": True, "deterministic": True}
