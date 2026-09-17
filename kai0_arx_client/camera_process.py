"""GUI camera acquisition and JPEG encoding in a fresh process, without an SDK instance."""
import multiprocessing
import os
import threading
import time
import numpy as np

ROLES=("head","left","right")
IMAGE_SHAPE=(3,480,640,3)


def _worker(connection, config, real, pixels, timestamps, frame_lock):
    cameras = None
    try:
        import io
        from PIL import Image
        from client import Cameras
        cameras = Cameras(config, real=real)
        shared=np.frombuffer(pixels,dtype=np.uint8).reshape(IMAGE_SHAPE)
        meta=np.frombuffer(timestamps,dtype=np.float64).reshape(3,4)
        sequence=0;ready=False
        while True:
            began=time.monotonic();images=cameras.read();sequence+=1
            with frame_lock:
                for i,role in enumerate(ROLES):
                    shared[i]=images[role]
                    m=cameras.frame_meta.get(role,{'captured':began,'received':time.monotonic(),'device_ms':began*1000,'sequence':sequence})
                    meta[i]=[m['captured'],m['received'],m['device_ms'],m['sequence']]
            if not ready:connection.send(('ready',os.getpid()));ready=True
            if not connection.poll():
                if not real:time.sleep(max(0,1/30-(time.monotonic()-began)))
                continue
            request = connection.recv()
            if request == 'close':
                break
            if request != 'read':
                raise ValueError('Unknown camera request')
            started = time.monotonic()
            frames = {}
            for name, rgb in images.items():
                output = io.BytesIO()
                Image.fromarray(rgb).save(output, format='JPEG', quality=70)
                frames[name] = output.getvalue()
            connection.send(('frame', (images, frames, round((time.monotonic()-started)*1000, 3))))
    except (EOFError, BrokenPipeError):
        pass
    except BaseException as exc:
        try:
            connection.send(('error', f'{type(exc).__name__}: {exc}'))
        except (EOFError, BrokenPipeError, OSError):
            pass
    finally:
        if cameras is not None:
            cameras.close()
        connection.close()


class CameraProcess:
    def __init__(self, config, real=True):
        # spawn is essential: never fork a live SDK or inherit its CAN threads.
        context = multiprocessing.get_context('spawn')
        self.connection, child = context.Pipe()
        self.lock = threading.Lock()
        self.closed = False
        self.pixels=context.RawArray('B',int(np.prod(IMAGE_SHAPE)))
        self.timestamps=context.RawArray('d',12);self.frame_lock=context.Lock()
        self.process = context.Process(target=_worker, args=(child, {'cameras': config['cameras']}, real,self.pixels,self.timestamps,self.frame_lock), daemon=True)
        try:
            self.process.start()
            child.close()
            self.pid = self._receive('ready', 8)
        except BaseException:
            child.close()
            self.close()
            raise

    def latest(self):
        if self.closed or not self.process.is_alive():raise RuntimeError('相机进程已退出')
        if not self.frame_lock.acquire(timeout=.005):raise RuntimeError('相机共享帧繁忙')
        try:
            pixels=np.frombuffer(self.pixels,dtype=np.uint8).reshape(IMAGE_SHAPE).copy()
            values=np.frombuffer(self.timestamps,dtype=np.float64).reshape(3,4).copy()
        finally:self.frame_lock.release()
        return {r:pixels[i] for i,r in enumerate(ROLES)}, {r:dict(zip(('captured','received','device_ms','sequence'),values[i].tolist())) for i,r in enumerate(ROLES)}

    def _receive(self, expected, timeout):
        if not self.connection.poll(timeout):
            raise TimeoutError(f'独立相机进程等待 {expected} 超时')
        try:
            kind, value = self.connection.recv()
        except EOFError as exc:
            raise RuntimeError('独立相机进程已退出') from exc
        if kind != expected:
            raise RuntimeError(f'独立相机进程错误：{value}')
        return value

    def read_preview(self):
        with self.lock:
            if self.closed:
                raise RuntimeError('相机进程已关闭')
            try:
                self.connection.send('read')
                return self._receive('frame', 1.2)
            except BaseException:
                # No stale frame or unread response is reused on another request.
                self.closed = True
                self._finish()
                raise

    def _finish(self):
        if self.process.pid is not None:
            if self.process.is_alive():
                try:
                    self.connection.send('close')
                except (OSError, EOFError):
                    pass
                self.process.join(.4)
            if self.process.is_alive():
                self.process.terminate()  # camera child only; never the SDK process
                self.process.join(.4)
            if self.process.is_alive():
                self.process.kill()
                self.process.join(.4)
        self.connection.close()

    def close(self):
        with self.lock:
            self.closed = True
            self._finish()
