from collections import Counter
from queue import Queue, Empty
import threading
from contextlib import nullcontext


class Cancelled(Exception):
    pass


class LaneFault(Exception):
    """Configuration/billing fault that ends the affected batch."""


class TaskRemoved(Cancelled):
    pass


class Lane:
    def __init__(self, name, workers, handler, on_state=lambda *args: None, guard=None, on_fault=lambda *args: None):
        self.name, self.handler, self.on_state = name, handler, on_state
        self.queue = Queue()
        self.enabled = threading.Event()
        self.enabled.set()
        self.cancelled = threading.Event()
        self.lock = threading.RLock()
        self.ids = set()
        self.pending = set()
        self.states = {}
        self.threads = []
        self.guard = guard or (lambda task, checkpoint: nullcontext())
        self.on_fault = on_fault
        self.fault_reason = ''
        self.tokens = {}
        for n in range(workers):
            thread = threading.Thread(target=self._worker, name=f'{name}-{n}', daemon=True)
            self.threads.append(thread)
            thread.start()

    def put(self, task):
        with self.lock:
            if self.cancelled.is_set():
                return False
            if task['id'] in self.pending:
                return False
            self.ids.add(task['id'])
            self.pending.add(task['id'])
            self.tokens[task['id']] = threading.Event()
            self._state(task, 'queued', '')
            self.queue.put(task)
            return True

    def _state(self, task, state, reason):
        with self.lock:
            self.states[task['id']] = state
        self.on_state(task, state, reason)

    def checkpoint(self):
        while not self.enabled.wait(0.15):
            if self.cancelled.is_set():
                raise Cancelled('本轮任务已取消')
        if self.cancelled.is_set():
            raise Cancelled('本轮任务已取消')

    def pause(self):
        self.enabled.clear()

    def resume(self):
        self.enabled.set()

    def stop(self):
        self.cancelled.set()
        self.enabled.set()
        while True:
            try:
                task = self.queue.get_nowait()
            except Empty:
                break
            state = 'removed' if self.tokens[task['id']].is_set() else 'interrupted'
            self._state(task, state, '未开始执行，已取消' if state == 'removed' else '本轮执行已结束，结果已保留')
            with self.lock:
                self.pending.discard(task['id'])
            self.queue.task_done()

    def remove(self, task_id):
        with self.lock:
            if task_id not in self.tokens:
                return False
            self.tokens[task_id].set()
            self.states[task_id] = 'removing' if self.states.get(task_id) == 'running' else 'removed'
            return True

    def task_checkpoint(self, task):
        while True:
            if self.tokens[task['id']].is_set():
                raise TaskRemoved('任务已取消，已有结果已保留')
            if self.cancelled.is_set():
                raise Cancelled('本轮执行已结束，已有结果已保留')
            if self.enabled.wait(.1):
                return

    def counts(self):
        with self.lock:
            return dict(Counter(self.states.values()))

    def _worker(self):
        while not self.cancelled.is_set():
            try:
                self.checkpoint()
                task = self.queue.get(timeout=0.2)
            except Empty:
                continue
            except Cancelled:
                return
            started = False
            try:
                checkpoint = lambda: self.task_checkpoint(task)
                checkpoint()
                with self.guard(task, checkpoint):
                    checkpoint()
                    self._state(task, 'running', '')
                    started = True
                    result = self.handler(task, checkpoint)
                    self._state(task, result or 'done', '')
            except LaneFault as exc:
                if self.tokens[task['id']].is_set():
                    self._state(task, 'removed', '任务已取消，已有结果已保留')
                else:
                    self.fault_reason = str(exc)
                    self._state(task, 'failed', str(exc))
                    self.stop()
                    self.on_fault(self.name, str(exc))
            except TaskRemoved as exc:
                self._state(task, 'removed', ('已取消，已有结果保留' if started else '未开始执行，已取消'))
            except Cancelled as exc:
                self._state(task, 'interrupted', ('处理中已停止，已有结果保留' if started else '未开始执行，已停止并保留'))
            except Exception as exc:
                self._state(task, 'failed', str(exc))
            finally:
                with self.lock:
                    self.pending.discard(task['id'])
                self.queue.task_done()

    def alive(self):
        return any(t.is_alive() for t in self.threads)

    def wait_stopped(self, timeout=3):
        import time
        deadline = time.monotonic() + timeout
        for thread in self.threads:
            thread.join(max(0, deadline - time.monotonic()))
