import _overlapped
import _winapi
from collections import deque

from . import BaseLoop, Future, BaseFile, BaseSocket

NULL = 0
INFINITE = 0xffffffff
ERROR_CONNECTION_REFUSED = 1225
ERROR_CONNECTION_ABORTED = 1236


class IOCPLoop(BaseLoop):
    def __init__(self, concurrency=INFINITE):
        super().__init__()
        self._port = _overlapped.CreateIoCompletionPort(_overlapped.INVALID_HANDLE_VALUE, NULL, 0, concurrency)
        self._current_iocp = dict()

    def _poll(self):
        if self._current_iocp:
            if not self._tasks or self._queue:
                if self._timers:
                    ms = max(100, (self._timers[0][0] - self.time()) * 1000)
                else:
                    ms = 100
            else:
                ms = 100
            while True:
                status = _overlapped.GetQueuedCompletionStatus(self._port, ms)  # See if anything is ready (LIFO)
                if status is None:
                    break
                ms = 0

                err, transferred, key, address = status

                try:
                    file = self._current_iocp.pop(address)
                    file._io_ready(transferred)  # Tell the file we're ready to perform IO
                except KeyError:
                    if key not in (0, _overlapped.INVALID_HANDLE_VALUE):
                        _winapi.CloseHandle(key)  # If we get a handle that doesn't exist or got deleted: Close it
                    continue
        else:
            self.sleep(max(0, self._timers[0][0] - self.time()))

    def wrap_file(self, file) -> "IOCPFile":
        """Wrap a file in an async file API."""
        overlap = _overlapped.Overlapped(NULL)  # Get a new overlapped object
        wrapped = IOCPFile(file, overlap)  # Create a new IOCPFile
        _overlapped.CreateIoCompletionPort(file.fileno(), self._port, 0, 0)  # Create a new port
        self._current_iocp[overlap.address] = wrapped  # Cache by address, which is all we'll have from the IOCP Status
        return wrapped

    def wrap_socket(self, socket) -> "IOCPSocket":
        """Wrap a file in an async socket API."""
        overlap = _overlapped.Overlapped(NULL)  # Refer to above, except for sockets not files :p
        wrapped = IOCPSocket(socket, overlap)
        _overlapped.CreateIoCompletionPort(socket.fileno(), self._port, 0, 0)
        self._current_iocp[overlap.address] = wrapped
        return wrapped


class IOCPFile(BaseFile):
    def __init__(self, file, overlap):
        self.file = file
        self._queue = deque()
        self._overlap = overlap

    def _io_ready(self, data):  # When we're ready to process IO
        if self._queue:
            _type, fut, _data = self._queue.pop()  # Apparently it processes LIFO? Or maybe I'm confused
            fut.set_result(data)

    def write(self, data):
        self._overlap.WriteFile(self.file.fileno(), data)  # Write our file data
        fut = Future()
        self._queue.append((0, fut, data))

        return fut

    async def read(self, nbytes):  # Read from file
        self._overlap.ReadFile(self.file.fileno(), nbytes)
        fut = Future()
        self._queue.append((1, fut, nbytes))
        await fut  # Ok this one is weird, we actually wait to be told we can read, rather than delegating the reading
        return self.file.read(nbytes)  # Like we do with writing

    def close(self):
        try:
            self._overlap.cancel()
            if self.file.fileno not in (0, _overlapped.INVALID_HANDLE_VALUE):  # As long as we aren't trying to close
                _winapi.CloseHandle(self.file.fileno())  # Stdout we should be good
        finally:
            self.file.close()

    @property
    def fileno(self):  # Get the fileno ... ?
        return self.file.fileno()


class IOCPSocket(BaseSocket):  # Its literally all the same, except send and recv not write and read
    def __init__(self, socket, overlap):
        self.file = socket
        self._queue = deque()
        self._overlap = overlap

    def _io_ready(self, data):
        if self._queue:
            _type, fut, _data = self._queue.popleft()
            fut.set_result(data)

    def send(self, data, flags=0):
        self._overlap.WSASend(self.file.fileno(), data, flags)
        fut = Future()
        self._queue.append((0, fut, data))

        return fut

    async def recv(self, nbytes, flags=0):
        self._overlap.WSARecv(self.file.fileno(), nbytes, flags)
        fut = Future()
        self._queue.append((1, fut, nbytes))
        await fut
        return self.file.recv(nbytes)

    def close(self):
        try:
            self._overlap.cancel()
            if self.file.fileno not in (0, _overlapped.INVALID_HANDLE_VALUE):
                _winapi.CloseHandle(self.file.fileno())
        finally:
            self.file.close()

    @property
    def fileno(self):
        return self.file.fileno()
