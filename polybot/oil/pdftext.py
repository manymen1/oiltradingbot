"""Resource-limited PDF text subprocess. Never executes PDF scripts or fetches URLs."""
from __future__ import annotations

import io
import json
import resource
import sys


def main():
    resource.setrlimit(resource.RLIMIT_AS, (384 * 1024 * 1024, 384 * 1024 * 1024))
    resource.setrlimit(resource.RLIMIT_CPU, (5, 5))
    from pypdf import PdfReader
    try:
        body = sys.stdin.buffer.read(8 * 1024 * 1024 + 1)
        if len(body) > 8 * 1024 * 1024:
            raise ValueError("oversize")
        reader = PdfReader(io.BytesIO(body))
        if reader.is_encrypted or len(reader.pages) > 20:
            raise ValueError("encrypted or too many pages")
        pages = []
        for page in reader.pages:
            contents = page.get_contents()
            if contents is not None and len(contents.get_data()) > 8 * 1024 * 1024:
                raise ValueError("oversize page stream")
            pages.append(page.extract_text() or "")
        text = "\n\n".join(pages).strip()
        if len(text) < 30 or len(text) > 100000:
            raise ValueError("text incomplete or too large; OCR/manual review required")
        print(json.dumps({"text": text, "pages": len(pages)}))
        return 0
    except Exception:
        print(json.dumps({"error": "PDF_TEXT_UNAVAILABLE"}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
