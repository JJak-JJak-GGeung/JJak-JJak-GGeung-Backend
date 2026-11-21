"""Compatibility import for existing callers; no OCR engine is used."""
from .documents import reconcile_documents

reconcile_ocr_documents = reconcile_documents
