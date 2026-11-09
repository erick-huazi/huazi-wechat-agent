"""Render dashboard quality states with temporary synthetic packs, never production data."""
from __future__ import annotations

import argparse
import subprocess
import threading
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from test_image_quality_gate import ImageQualityTests, dashboard, quality, write_json


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--node", required=True)
    parser.add_argument("--modules", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    fixture = ImageQualityTests()
    fixture.setUp()
    snapshots = {}
    pack = fixture.pack
    server = None
    try:
        write_json(pack / "image-quality-report.json", quality.build_report(pack))
        snapshots["verified"] = dashboard.dashboard_payload(pack.parent, pack.name)
        fixture.review["items"][0]["sha256"] = "0" * 64
        fixture.save_review()
        snapshots["stale"] = dashboard.dashboard_payload(pack.parent, pack.name)
        (pack / quality.INPUT_FILES[1]).unlink()
        snapshots["unverified"] = dashboard.dashboard_payload(pack.parent, pack.name)

        class Handler(dashboard.DashboardHandler):
            def do_GET(self):
                parsed = urlparse(self.path)
                if parsed.path == "/api/dashboard":
                    case = parse_qs(parsed.query).get("case", ["verified"])[0]
                    self.send_json(snapshots[case])
                else:
                    super().do_GET()

            def do_POST(self):
                self.send_error(405)

            def log_message(self, *_):
                pass

        server = dashboard.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        server.outputs_root = pack.parent
        server.public_url = f"http://127.0.0.1:{server.server_address[1]}"
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        args.output.mkdir(parents=True, exist_ok=True)
        subprocess.run([args.node, str(Path(__file__).with_suffix(".cjs")), args.modules,
                        server.public_url, str(args.output.resolve())], check=True, timeout=120)
    finally:
        if server:
            server.shutdown()
            server.server_close()
        fixture.doCleanups()


if __name__ == "__main__":
    main()
