#!/usr/bin/env python3
"""Same-user local control for a CLI client whose terminal has closed."""
import argparse
import json
import os
from pathlib import Path
import socket
import stat
import threading


def socket_path():
    root = Path(os.environ.get('XDG_RUNTIME_DIR', f'/run/user/{os.getuid()}'))
    if not root.is_dir(): root = Path('/tmp')
    return root / f'kai0-client-control-{os.getuid()}.sock'


class Control:
    def __init__(self, pause):
        self.pause = pause
        self.stop = threading.Event(); self.release = threading.Event()
        self.finished = threading.Event(); self.holding = False
        self.path = socket_path()
        if self.path.exists():
            info = self.path.lstat()
            if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid():
                raise RuntimeError('Unexpected CLI control socket owner/type')
            with socket.socket(socket.AF_UNIX) as probe:
                try: probe.connect(str(self.path))
                except ConnectionRefusedError: self.path.unlink()
                else: raise RuntimeError('Another CLI control socket is active')
        self.sock = socket.socket(socket.AF_UNIX)
        try:
            self.sock.bind(str(self.path)); self.path.chmod(0o600)
            self.sock.listen(4); self.sock.settimeout(.2)
        except BaseException:
            self.sock.close(); raise
        self.thread = threading.Thread(target=self.serve, daemon=True)
        self.thread.start()

    def handle(self, request):
        action = request.get('action')
        if action == 'pause': self.pause.set()
        elif action == 'stop': self.stop.set()
        elif action == 'release':
            if not self.holding or request.get('supported') is not True:
                raise ValueError('仅在保持状态且确认双臂已支撑后可释放')
            self.release.set()
        elif action != 'status': raise ValueError('Unknown action')
        return {'ok': True, 'holding': self.holding, 'stop_requested': self.stop.is_set(),
                'release_requested': self.release.is_set(), 'pid': os.getpid()}

    def serve(self):
        while not self.finished.is_set():
            try: conn, _ = self.sock.accept()
            except socket.timeout: continue
            except OSError: break
            with conn:
                conn.settimeout(.5)
                try:
                    payload=b''
                    while b'\n' not in payload and len(payload)<4096:
                        chunk=conn.recv(4096-len(payload))
                        if not chunk: break
                        payload+=chunk
                    answer=self.handle(json.loads(payload))
                except Exception as exc: answer={'ok': False, 'error': str(exc)}
                try: conn.sendall(json.dumps(answer).encode()+b'\n')
                except OSError: pass

    def close(self):
        self.finished.set(); self.sock.close(); self.thread.join(1)
        self.path.unlink(missing_ok=True)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['status','pause','stop','release'])
    parser.add_argument('--supported', action='store_true', help='确认双臂已放稳/支撑，允许释放控制')
    args=parser.parse_args()
    if args.action=='release' and not args.supported: parser.error('释放需 --supported')
    with socket.socket(socket.AF_UNIX) as sock:
        sock.settimeout(2); sock.connect(str(socket_path()))
        sock.sendall(json.dumps(vars(args)).encode()+b'\n')
        response=json.loads(sock.recv(4096))
        print(json.dumps(response,ensure_ascii=False))
        if not response.get('ok'): raise SystemExit(1)


if __name__=='__main__':main()
