"""Regression guard: every photo entry point is GPT-only, including failures."""
import ast
import builtins
from pathlib import Path
import unittest
from unittest.mock import patch

from . import extract_document
from . import vision
from .extractor import ExtractionError
from .test_extractor import image_bytes, provider


class NoOcrTests(unittest.TestCase):
    def test_no_runtime_imports_or_ocr_engine_definitions(self):
        forbidden = {"rapidocr", "onnxruntime", "cv2", "pytesseract", "easyocr", "paddleocr"}
        for path in Path(__file__).parents[1].rglob("*.py"):
            if path.name.startswith("test_"):
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                names = []
                if isinstance(node, ast.Import):
                    names = [alias.name.split(".")[0] for alias in node.names]
                elif isinstance(node, ast.ImportFrom):
                    names = [(node.module or "").split(".")[0]]
                self.assertFalse(set(names) & forbidden, str(path))
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    self.assertNotIn(node.name, {"read_ocr", "_engine"}, str(path))

    def test_public_and_legacy_photo_entry_points_are_same_function(self):
        self.assertIs(vision.extract_document, extract_document)

    def test_success_and_failure_never_import_an_ocr_package(self):
        original_import = builtins.__import__
        attempts = []

        def guarded(name, *args, **kwargs):
            if name.split(".")[0] in {"rapidocr", "onnxruntime", "cv2"}:
                attempts.append(name)
                raise AssertionError("OCR import attempted")
            return original_import(name, *args, **kwargs)

        client = provider()
        with patch("builtins.__import__", side_effect=guarded):
            extract_document(image_bytes(), "image/png", None, client=client)
            client.responses.create.side_effect = RuntimeError("provider failure")
            with self.assertRaises(ExtractionError):
                extract_document(image_bytes(), "image/png", None, client=client)
        self.assertEqual(attempts, [])

    def test_runtime_requirements_and_docker_have_no_ocr_bootstrap(self):
        root = Path(__file__).resolve().parents[2]
        for relative in ("requirements.txt", "app/photo/requirements.txt", "Dockerfile"):
            text = (root / relative).read_text(encoding="utf-8").lower()
            for forbidden in ("rapidocr", "onnxruntime", "opencv", "cv2", "_engine("):
                self.assertNotIn(forbidden, text, relative)


if __name__ == "__main__":
    unittest.main()
