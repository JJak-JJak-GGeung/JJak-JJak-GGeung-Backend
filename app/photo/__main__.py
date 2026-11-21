"""Run a real image reading: python -m app.photo image.png --output reading.json."""
import argparse
import json
import mimetypes
from pathlib import Path
import sys

from .extractor import extract_document


def main():
    parser = argparse.ArgumentParser(description="GPT-only document reading; does not save to DB")
    parser.add_argument("image", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    try:
        mime = mimetypes.guess_type(args.image.name)[0]
        result = extract_document(args.image.read_bytes(), mime, None)
        args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as error:
        # Provider exception bodies may contain sensitive request data.
        print(f"Reading failed ({type(error).__name__}); check dependencies, server key, model and connectivity.", file=sys.stderr)
        return 1
    print(f"Saved unconfirmed reading to {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
