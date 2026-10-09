import hashlib
import math
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from agenthub.processing.semantic import (
    DIMENSION,
    MODEL_KEY,
    ModelAsset,
    ModelSpec,
    SemanticModel,
    SemanticSetupError,
    SemanticUnavailable,
    _nomic_postprocess,
    check_conformance,
    model_directory,
    normalize_vector,
    prepare_document,
    prepare_query,
    setup_model,
    verify_model_directory,
)


def fixture_spec():
    model = b"small offline model fixture"
    return ModelSpec(
        model_id="fixture/semantic",
        revision="a" * 40,
        dimension=3,
        native_dimension=4,
        model_file="onnx/model.onnx",
        tokenizer_file="tokenizer.json",
        assets=(
            ModelAsset("onnx/model.onnx", hashlib.sha256(model).hexdigest(), 100),
            ModelAsset("tokenizer.json", None, 100),
        ),
    ), {"onnx/model.onnx": model, "tokenizer.json": b"{}"}


class RecordingRuntime:
    def __init__(self):
        self.inputs = []

    def encode(self, texts):
        self.inputs.append(list(texts))
        vectors = []
        for text in texts:
            seed = 1.0 if text.startswith("search_document: ") else 2.0
            vectors.append(normalize_vector([seed, 1.0, -1.0], 3))
        return vectors


class FakeConformingModel:
    model_key = "fixture@one"
    dimension = 3

    def embed_documents(self, texts):
        return [normalize_vector([1.0, 2.0, 3.0], 3) for _ in texts]

    def embed_queries(self, texts):
        return [normalize_vector([3.0, 2.0, 1.0], 3) for _ in texts]


class SemanticModelTests(unittest.TestCase):
    def test_prefixes_normalization_and_vector_helpers(self):
        self.assertEqual(prepare_document("  retry failures  "),
                         "search_document: retry failures")
        self.assertEqual(prepare_query("  how do retries work?  "),
                         "search_query: how do retries work?")
        with self.assertRaises(ValueError):
            prepare_query("  ")
        vector = normalize_vector([3.0, 4.0])
        self.assertAlmostEqual(math.sqrt(sum(value * value for value in vector)), 1.0)
        processed = _nomic_postprocess(list(range(768)))
        self.assertEqual(len(processed), DIMENSION)
        self.assertAlmostEqual(math.sqrt(sum(value * value for value in processed)), 1.0)

    def test_missing_model_fails_cleanly_without_optional_imports(self):
        with tempfile.TemporaryDirectory() as home:
            with patch("agenthub.processing.semantic.urlopen") as network:
                with self.assertRaises(SemanticUnavailable) as caught:
                    SemanticModel.from_home(home)
                self.assertEqual(caught.exception.code, "semantic_model_not_installed")
                model = SemanticModel.from_home(home, require_files=False)
                with self.assertRaises(SemanticUnavailable) as caught:
                    model.embed_queries(["local only"])
                self.assertEqual(caught.exception.code, "semantic_model_not_installed")
                network.assert_not_called()

    def test_explicit_setup_is_private_verified_atomic_and_idempotent(self):
        spec, payloads = fixture_spec()
        calls = []

        def download(url, destination, maximum):
            calls.append(url)
            data = payloads[next(asset.path for asset in spec.assets if url.endswith(asset.path))]
            self.assertLessEqual(len(data), maximum)
            destination.write_bytes(data)

        with tempfile.TemporaryDirectory() as home:
            result = setup_model(home, spec=spec, downloader=download)
            self.assertTrue(result["installed"])
            directory = model_directory(home, spec)
            manifest = verify_model_directory(directory, spec)
            self.assertEqual(manifest["model_key"], spec.key)
            self.assertEqual(len(calls), 2)
            second = setup_model(home, spec=spec, downloader=lambda *_: self.fail("downloaded"))
            self.assertFalse(second["installed"])
            if hasattr(Path(".").stat(), "st_mode"):
                self.assertEqual(directory.stat().st_mode & 0o077, 0)
                self.assertEqual((directory / spec.model_file).stat().st_mode & 0o077, 0)

    def test_bad_setup_checksum_leaves_no_install(self):
        spec, payloads = fixture_spec()

        def corrupt(url, destination, maximum):
            destination.write_bytes(b"wrong")

        with tempfile.TemporaryDirectory() as home:
            with self.assertRaises(SemanticSetupError) as caught:
                setup_model(home, spec=spec, downloader=corrupt)
            self.assertEqual(caught.exception.code, "semantic_model_checksum_mismatch")
            self.assertFalse(model_directory(home, spec).exists())

    def test_model_api_applies_distinct_prefixes(self):
        spec, payloads = fixture_spec()

        def download(url, destination, maximum):
            destination.write_bytes(payloads[next(a.path for a in spec.assets if url.endswith(a.path))])

        with tempfile.TemporaryDirectory() as home:
            setup_model(home, spec=spec, downloader=download)
            model = SemanticModel.from_home(home, spec=spec)
            runtime = RecordingRuntime()
            model._runtime = runtime
            documents = model.embed_documents(["same words"])
            queries = model.embed_queries(["same words"])
            self.assertEqual(runtime.inputs,
                             [["search_document: same words"], ["search_query: same words"]])
            self.assertEqual(len(documents[0]), model.dimension)
            self.assertNotEqual(documents, queries)

    def test_backend_conformance_without_downloaded_model(self):
        report = check_conformance(FakeConformingModel())
        self.assertEqual(report, {"model_key": "fixture@one", "dimension": 3,
                                  "normalized": True, "deterministic": True})



if __name__ == "__main__":
    unittest.main()
