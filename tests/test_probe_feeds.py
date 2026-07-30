from __future__ import annotations

import os
import subprocess
from pathlib import Path


def test_probe_counts_minified_xml_and_distinguishes_json(
    tmp_path: Path,
) -> None:
    fixtures = tmp_path / "fixtures"
    fixtures.mkdir()
    (fixtures / "xml").write_text(
        "<rss><item><title>a</title></item><item ><title>b</title></item></rss>",
        encoding="utf-8",
    )
    (fixtures / "empty").write_text("<rss></rss>", encoding="utf-8")
    (fixtures / "json").write_text('{"items": []}', encoding="utf-8")
    (fixtures / "missing").write_text("not found", encoding="utf-8")

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_curl = fake_bin / "curl"
    fake_curl.write_text(
        """#!/usr/bin/env bash
out=""
url=""
while [ "$#" -gt 0 ]; do
  case "$1" in
    -o)
      out="$2"
      shift 2
      ;;
    -w|-A|--max-time)
      shift 2
      ;;
    -s|-L)
      shift
      ;;
    *)
      url="$1"
      shift
      ;;
  esac
done
case "$url" in
  *minified*) body="xml"; code="200" ;;
  *empty*) body="empty"; code="200" ;;
  *json*) body="json"; code="200" ;;
  *) body="missing"; code="404" ;;
esac
cp "$FIXTURE_DIR/$body" "$out"
printf '%s' "$code"
""",
        encoding="utf-8",
    )
    fake_curl.chmod(0o755)
    urls = tmp_path / "urls.txt"
    urls.write_text(
        "\n".join(
            [
                "https://example.test/minified",
                "https://example.test/empty",
                "https://example.test/json",
                "https://example.test/missing",
            ]
        ),
        encoding="utf-8",
    )
    env = {
        **os.environ,
        "FIXTURE_DIR": str(fixtures),
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
    }

    result = subprocess.run(
        ["bash", "scripts/probe_feeds.sh", str(urls)],
        cwd=Path(__file__).parents[1],
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )

    lines = result.stdout.splitlines()
    assert lines[0].split()[:3] == ["LIVE", "200", "items=2"]
    assert lines[1].split()[:3] == ["dead", "200", "items=0"]
    assert lines[2].split()[:3] == ["JSON", "200", "items=0"]
    assert lines[3].split()[:3] == ["dead", "404", "items=0"]
