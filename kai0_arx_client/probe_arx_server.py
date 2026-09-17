"""Read-only ARX protocol probe. Never initializes SDK hardware or sends motion commands."""

import argparse
import io
import json
from pathlib import Path
import time
import urllib.request

import numpy as np
from PIL import Image
from websockets.sync.client import connect

try:
    from openpi_client import image_tools
    from openpi_client import msgpack_numpy
except ImportError:
    from vendor import image_tools
    from vendor import msgpack_numpy


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--server", default="ws://192.168.2.117:8000")
    parser.add_argument("--station", help="Existing station HTTP URL, e.g. http://127.0.0.1:8090")
    parser.add_argument("--count", type=int, default=5)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.count < 1:
        parser.error("--count must be positive")
    names = {"head": "top_head", "left": "hand_left", "right": "hand_right"}
    obs = {
        "state": np.array([0, 0.5, 0.8, 0, 0, 0, -0.5] * 2, np.float32),
        "images": {name: np.zeros((3, 224, 224), np.uint8) for name in names.values()},
        "prompt": "communication test only",
    }
    report = {
        "server": args.server,
        "motion_executed": False,
        "source": "station JPEG previews and feedback (not synchronized)" if args.station else "synthetic",
    }
    if args.station:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        for role, name in names.items():
            with opener.open(args.station.rstrip("/") + "/stream/" + role, timeout=3) as response:
                data = bytearray()
                end = -1
                deadline = time.monotonic() + 5
                while len(data) < 2_000_000 and time.monotonic() < deadline:
                    block = response.read(4096)
                    if not block:
                        break
                    data.extend(block)
                    begin = data.find(b"\xff\xd8")
                    end = data.find(b"\xff\xd9", begin + 2) if begin >= 0 else -1
                    if end >= 0:
                        rgb = np.asarray(Image.open(io.BytesIO(bytes(data[begin : end + 2]))).convert("RGB"))
                        obs["images"][name] = np.ascontiguousarray(
                            image_tools.resize_with_pad(rgb, 224, 224).transpose(2, 0, 1)
                        )
                        break
                else:
                    raise RuntimeError(f"Timed out reading {role}")
                if end < 0:
                    raise RuntimeError(f"Missing JPEG for {role}")
        with opener.open(args.station.rstrip("/") + "/api/status", timeout=3) as response:
            status = json.load(response)
        obs["state"] = np.asarray(status["arm"]["positions"], np.float32)
        report["station_arm"] = status["arm"]
        report["recording_state"] = status["recording"]["state"]
    timings = []
    with connect(
        args.server, proxy=None, compression=None, max_size=32 * 1024 * 1024, open_timeout=5, close_timeout=1
    ) as ws:
        metadata = msgpack_numpy.unpackb(ws.recv(timeout=5))
        report["metadata"] = metadata
        for _ in range(args.count):
            start = time.monotonic()
            ws.send(msgpack_numpy.packb(obs))
            raw = ws.recv(timeout=30)
            if isinstance(raw, str):
                raise RuntimeError(raw)
            result = msgpack_numpy.unpackb(raw)
            timings.append((time.monotonic() - start) * 1000)
            actions = np.asarray(result["actions"])
            if actions.shape != (metadata["action_horizon"], 14) or not np.isfinite(actions).all():
                raise ValueError("Invalid action shape or nonfinite values")
            if metadata.get("mode") == "mock":
                np.testing.assert_array_equal(actions, np.repeat(obs["state"][None], len(actions), axis=0))
    report.update(
        passed=True,
        requests=args.count,
        action_shape=list(actions.shape),
        images={k: list(v.shape) for k, v in obs["images"].items()},
        round_trip_ms=timings,
        mean_round_trip_ms=float(np.mean(timings)),
    )
    rendered = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered + "\n")
    print(rendered)


if __name__ == "__main__":
    main()
