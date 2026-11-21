"""Compatibility entry point for the GPT-only extractor; no supplemental OCR."""
from .extractor import extract_document, iso
import re
from typing import Literal

DocType = Literal["DELIVERY", "RECEIPT", "INVOICE", "CORRECTED_INVOICE"]


def decimal_text(value):
    if value is None or not isinstance(value, str) or len(value) > 32:
        return None
    return value if re.fullmatch(r"-?[0-9]{1,16}(?:\.[0-9]{1,8})?", value) else None

__all__ = ["extract_document", "iso", "DocType", "decimal_text"]
