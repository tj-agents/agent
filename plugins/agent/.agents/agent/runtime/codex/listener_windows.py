"""Windows process ownership and in-memory listener environment transfer (Python 3.11+)."""

from __future__ import annotations

import ctypes
from ctypes import wintypes
import functools
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import subprocess
import time
from typing import Any, BinaryIO

from codex.listener_types import BackendIdentity, CommandResult

class SecurityAttributes(ctypes.Structure):
    _fields_ = [('length', wintypes.DWORD), ('descriptor', wintypes.LPVOID), ('inherit', wintypes.BOOL)]


class StartupInfo(ctypes.Structure):
    _fields_ = [('cb', wintypes.DWORD), ('reserved', wintypes.LPWSTR), ('desktop', wintypes.LPWSTR),
                ('title', wintypes.LPWSTR), ('x', wintypes.DWORD), ('y', wintypes.DWORD),
                ('x_size', wintypes.DWORD), ('y_size', wintypes.DWORD),
                ('x_count', wintypes.DWORD), ('y_count', wintypes.DWORD),
                ('fill', wintypes.DWORD), ('flags', wintypes.DWORD),
                ('show', wintypes.WORD), ('reserved_size', wintypes.WORD),
                ('reserved_bytes', ctypes.POINTER(ctypes.c_ubyte)),
                ('stdin', wintypes.HANDLE), ('stdout', wintypes.HANDLE), ('stderr', wintypes.HANDLE)]


class StartupInfoEx(ctypes.Structure):
    _fields_ = [('startup', StartupInfo), ('attributes', wintypes.LPVOID)]


class ProcessInformation(ctypes.Structure):
    _fields_ = [('process', wintypes.HANDLE), ('thread', wintypes.HANDLE),
                ('pid', wintypes.DWORD), ('thread_id', wintypes.DWORD)]


class BasicLimits(ctypes.Structure):
    _fields_ = [('process_time', ctypes.c_int64), ('job_time', ctypes.c_int64),
                ('flags', wintypes.DWORD), ('minimum_working_set', ctypes.c_size_t),
                ('maximum_working_set', ctypes.c_size_t), ('active_process_limit', wintypes.DWORD),
                ('affinity', ctypes.c_size_t), ('priority_class', wintypes.DWORD),
                ('scheduling_class', wintypes.DWORD)]


class IoCounters(ctypes.Structure):
    _fields_ = [(name, ctypes.c_uint64) for name in
                ('read_operations', 'write_operations', 'other_operations',
                 'read_bytes', 'write_bytes', 'other_bytes')]


class ExtendedLimits(ctypes.Structure):
    _fields_ = [('basic', BasicLimits), ('io', IoCounters), ('process_memory', ctypes.c_size_t),
                ('job_memory', ctypes.c_size_t), ('peak_process_memory', ctypes.c_size_t),
                ('peak_job_memory', ctypes.c_size_t)]


class JobAccounting(ctypes.Structure):
    _fields_ = [('total_user_time', ctypes.c_int64), ('total_kernel_time', ctypes.c_int64),
                ('period_user_time', ctypes.c_int64), ('period_kernel_time', ctypes.c_int64),
                ('page_faults', wintypes.DWORD), ('total_processes', wintypes.DWORD),
                ('active_processes', wintypes.DWORD), ('terminated_processes', wintypes.DWORD)]


class Overlapped(ctypes.Structure):
    _fields_ = [('internal', ctypes.c_size_t), ('internal_high', ctypes.c_size_t),
                ('offset', wintypes.DWORD), ('offset_high', wintypes.DWORD), ('event', wintypes.HANDLE)]


@functools.lru_cache(maxsize=1)
def api() -> tuple[Any, Any]:
    if os.name != 'nt':
        raise RuntimeError('Windows listener helpers require Windows')
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    security = ctypes.WinDLL('advapi32', use_last_error=True)
    pointer = wintypes.LPVOID
    signatures = {
        'CloseHandle': ([wintypes.HANDLE], wintypes.BOOL),
        'GetCurrentProcess': ([], wintypes.HANDLE),
        'OpenProcess': ([wintypes.DWORD, wintypes.BOOL, wintypes.DWORD], wintypes.HANDLE),
        'GetProcessTimes': ([wintypes.HANDLE, pointer, pointer, pointer, pointer], wintypes.BOOL),
        'QueryFullProcessImageNameW': ([wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, pointer], wintypes.BOOL),
        'CreateJobObjectW': ([pointer, wintypes.LPCWSTR], wintypes.HANDLE),
        'SetInformationJobObject': ([wintypes.HANDLE, ctypes.c_int, pointer, wintypes.DWORD], wintypes.BOOL),
        'QueryInformationJobObject': ([wintypes.HANDLE, ctypes.c_int, pointer,
                                       wintypes.DWORD, pointer], wintypes.BOOL),
        'TerminateJobObject': ([wintypes.HANDLE, wintypes.UINT], wintypes.BOOL),
        'DuplicateHandle': ([wintypes.HANDLE, wintypes.HANDLE, wintypes.HANDLE,
                             pointer, wintypes.DWORD, wintypes.BOOL, wintypes.DWORD], wintypes.BOOL),
        'InitializeProcThreadAttributeList': ([pointer, wintypes.DWORD, wintypes.DWORD, pointer], wintypes.BOOL),
        'UpdateProcThreadAttribute': ([pointer, wintypes.DWORD, ctypes.c_size_t, pointer,
                                      ctypes.c_size_t, pointer, pointer], wintypes.BOOL),
        'DeleteProcThreadAttributeList': ([pointer], None),
        'CreateProcessW': ([wintypes.LPCWSTR, wintypes.LPWSTR, pointer, pointer, wintypes.BOOL,
                            wintypes.DWORD, pointer, wintypes.LPCWSTR, pointer, pointer], wintypes.BOOL),
        'WaitForSingleObject': ([wintypes.HANDLE, wintypes.DWORD], wintypes.DWORD),
        'GetExitCodeProcess': ([wintypes.HANDLE, pointer], wintypes.BOOL),
        'CreateNamedPipeW': ([wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD,
                              wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, pointer], wintypes.HANDLE),
        'CreateFileW': ([wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, pointer,
                         wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE], wintypes.HANDLE),
        'ConnectNamedPipe': ([wintypes.HANDLE, pointer], wintypes.BOOL),
        'ReadFile': ([wintypes.HANDLE, pointer, wintypes.DWORD, pointer, pointer], wintypes.BOOL),
        'WriteFile': ([wintypes.HANDLE, pointer, wintypes.DWORD, pointer, pointer], wintypes.BOOL),
        'CreateEventW': ([pointer, wintypes.BOOL, wintypes.BOOL, wintypes.LPCWSTR], wintypes.HANDLE),
        'GetOverlappedResult': ([wintypes.HANDLE, pointer, pointer, wintypes.BOOL], wintypes.BOOL),
        'CancelIoEx': ([wintypes.HANDLE, pointer], wintypes.BOOL),
        'LocalFree': ([pointer], pointer),
    }
    for name, (arguments, result) in signatures.items():
        function = getattr(kernel, name)
        function.argtypes, function.restype = arguments, result
    for name, arguments in {
        'OpenProcessToken': [wintypes.HANDLE, wintypes.DWORD, pointer],
        'GetTokenInformation': [wintypes.HANDLE, ctypes.c_int, pointer, wintypes.DWORD, pointer],
        'ConvertSidToStringSidW': [pointer, pointer],
        'ConvertStringSecurityDescriptorToSecurityDescriptorW': [wintypes.LPCWSTR, wintypes.DWORD,
                                                                pointer, pointer],
    }.items():
        function = getattr(security, name)
        function.argtypes, function.restype = arguments, wintypes.BOOL
    return kernel, security


def checked(succeeded: Any) -> Any:
    if not succeeded:
        raise ctypes.WinError(ctypes.get_last_error())
    return succeeded


def daemon_executable(profile: str, backend: BackendIdentity) -> str:
    from codex import client

    kernel, _security = api()
    record_path = Path(profile) / 'app-server-daemon' / 'daemon.pid'
    record = json.loads(record_path.read_text(encoding='utf-8'))
    if (not isinstance(record, dict) or record.get('pid') != backend.pid
            or str(record.get('processStartTime')) != backend.process_start_time):
        raise RuntimeError('The pinned native Codex daemon metadata changed')
    process = checked(kernel.OpenProcess(0x1000 | 0x100000, False, backend.pid))
    try:
        creation, exited, system, user = (wintypes.FILETIME() for _ in range(4))
        checked(kernel.GetProcessTimes(process, ctypes.byref(creation), ctypes.byref(exited),
                                       ctypes.byref(system), ctypes.byref(user)))
        started = str((creation.dwHighDateTime << 32) | creation.dwLowDateTime)
        if started != backend.process_start_time or kernel.WaitForSingleObject(process, 0) != 258:
            raise RuntimeError('The pinned native Codex daemon process is stale or reused')
        size = wintypes.DWORD(32768)
        image = ctypes.create_unicode_buffer(size.value)
        checked(kernel.QueryFullProcessImageNameW(process, 0, image, ctypes.byref(size)))
        context = client.process_context(backend.pid, include_command_line=True)
        arguments = context.get('arguments')
        if (not isinstance(arguments, list) or not arguments
                or any(not isinstance(argument, str) for argument in arguments)
                or client.command_name(arguments[1:]) != 'app-server'):
            raise RuntimeError('The pinned native Codex process is not an app-server daemon')
        if (not isinstance(context.get('profile'), str)
                or os.path.normcase(str(Path(context['profile']).resolve()))
                != os.path.normcase(str(Path(profile).resolve()))
                or context.get(client.RECORD_ENV) or context.get(client.RECORD_PATH_ENV)):
            raise RuntimeError('The pinned native Codex daemon profile or recording context changed')
        if (kernel.WaitForSingleObject(process, 0) != 258
                or json.loads(record_path.read_text(encoding='utf-8')) != record):
            raise RuntimeError('The pinned native Codex daemon changed during validation')
        return str(Path(image.value).resolve(strict=True))
    finally:
        kernel.CloseHandle(process)


def terminate_owned_processes(job: Any) -> None:
    kernel, _security = api()
    checked(kernel.TerminateJobObject(job, 1))
    deadline = time.monotonic() + 5
    while True:
        accounting = JobAccounting()
        checked(kernel.QueryInformationJobObject(job, 1, ctypes.byref(accounting),
                                                 ctypes.sizeof(accounting), None))
        if accounting.active_processes == 0:
            return
        if time.monotonic() >= deadline:
            raise RuntimeError('The Windows listener process tree did not terminate')
        time.sleep(.02)


def run(command: tuple[str, ...], cwd: str, timeout: float, output: BinaryIO,
        environment: dict[str, str]) -> CommandResult:
    import msvcrt

    kernel, _security = api()
    job = checked(kernel.CreateJobObjectW(None, None))
    process = ProcessInformation()
    attributes = None
    duplicates: list[Any] = []
    try:
        limits = ExtendedLimits()
        limits.basic.flags = 0x2000
        checked(kernel.SetInformationJobObject(job, 9, ctypes.byref(limits), ctypes.sizeof(limits)))
        size = ctypes.c_size_t()
        kernel.InitializeProcThreadAttributeList(None, 2, 0, ctypes.byref(size))
        storage = ctypes.create_string_buffer(size.value)
        checked(kernel.InitializeProcThreadAttributeList(storage, 2, 0, ctypes.byref(size)))
        attributes = storage
        jobs = (wintypes.HANDLE * 1)(job)
        checked(kernel.UpdateProcThreadAttribute(attributes, 0, 0x2000D, jobs,
                                                 ctypes.sizeof(jobs), None, None))
        with open(os.devnull, 'rb') as null:
            for descriptor in (null.fileno(), output.fileno()):
                handle = wintypes.HANDLE()
                checked(kernel.DuplicateHandle(kernel.GetCurrentProcess(), msvcrt.get_osfhandle(descriptor),
                                                kernel.GetCurrentProcess(), ctypes.byref(handle), 0, True, 2))
                duplicates.append(handle)
            inherited = (wintypes.HANDLE * 2)(*(handle.value for handle in duplicates))
            checked(kernel.UpdateProcThreadAttribute(attributes, 0, 0x20002, inherited,
                                                     ctypes.sizeof(inherited), None, None))
            startup = StartupInfoEx()
            startup.startup.cb = ctypes.sizeof(startup)
            startup.startup.flags = 0x100
            startup.startup.stdin, startup.startup.stdout = duplicates
            startup.startup.stderr = duplicates[1]
            startup.attributes = ctypes.cast(attributes, wintypes.LPVOID)
            executable = command[0]
            if not Path(executable).is_absolute():
                if os.path.dirname(executable):
                    executable = str((Path(cwd) / executable).resolve())
                else:
                    executable = shutil.which(executable, path=environment.get('PATH', '')) or executable
            environment_block = ctypes.create_unicode_buffer(
                '\0'.join(f'{key}={value}' for key, value in sorted(environment.items(),
                                                                   key=lambda item: item[0].upper())) + '\0\0')
            checked(kernel.CreateProcessW(executable, ctypes.create_unicode_buffer(subprocess.list2cmdline(command)),
                                           None, None, True, 0x08000000 | 0x80000 | 0x400,
                                           environment_block, cwd, ctypes.byref(startup), ctypes.byref(process)))
        result = kernel.WaitForSingleObject(process.process, min(0xFFFFFFFE, math_milliseconds(timeout)))
        timed_out = result == 258
        if timed_out:
            checked(kernel.TerminateJobObject(job, 1))
            if kernel.WaitForSingleObject(process.process, 5000) != 0:
                raise RuntimeError('The timed-out Windows command did not terminate')
        elif result != 0:
            raise ctypes.WinError(ctypes.get_last_error())
        code = wintypes.DWORD()
        checked(kernel.GetExitCodeProcess(process.process, ctypes.byref(code)))
        return CommandResult(code.value, timed_out)
    finally:
        try:
            terminate_owned_processes(job)
        finally:
            kernel.CloseHandle(job)
            for handle in (process.thread, process.process, *duplicates):
                if handle:
                    kernel.CloseHandle(handle)
            if attributes is not None:
                kernel.DeleteProcThreadAttributeList(attributes)


def math_milliseconds(seconds: float) -> int:
    return max(1, int(seconds * 1000))


def current_user_sid() -> str:
    kernel, security = api()
    token = wintypes.HANDLE()
    checked(security.OpenProcessToken(kernel.GetCurrentProcess(), 8, ctypes.byref(token)))
    try:
        size = wintypes.DWORD()
        security.GetTokenInformation(token, 1, None, 0, ctypes.byref(size))
        storage = ctypes.create_string_buffer(size.value)
        checked(security.GetTokenInformation(token, 1, storage, size.value, ctypes.byref(size)))
        sid = ctypes.cast(storage, ctypes.POINTER(wintypes.LPVOID))[0]
        text = wintypes.LPWSTR()
        checked(security.ConvertSidToStringSidW(sid, ctypes.byref(text)))
        try:
            return text.value or ''
        finally:
            kernel.LocalFree(text)
    finally:
        kernel.CloseHandle(token)


def overlapped_io(handle: Any, operation: str, data: bytes | int | None, deadline: float) -> bytes:
    kernel, _security = api()
    overlapped = Overlapped()
    overlapped.event = checked(kernel.CreateEventW(None, True, False, None))
    buffer = ctypes.create_string_buffer(data if isinstance(data, bytes) else (data or 1))
    transferred = wintypes.DWORD()
    try:
        if operation == 'connect':
            succeeded = kernel.ConnectNamedPipe(handle, ctypes.byref(overlapped))
        else:
            function = kernel.WriteFile if operation == 'write' else kernel.ReadFile
            length = len(data) if isinstance(data, bytes) else data
            succeeded = function(handle, buffer, length, ctypes.byref(transferred), ctypes.byref(overlapped))
        error = ctypes.get_last_error()
        if operation == 'connect' and (succeeded or error == 535):
            return b''
        if not succeeded and error != 997:
            raise ctypes.WinError(error)
        remaining = deadline - time.monotonic()
        if remaining <= 0 or kernel.WaitForSingleObject(overlapped.event, math_milliseconds(remaining)) != 0:
            kernel.CancelIoEx(handle, ctypes.byref(overlapped))
            kernel.GetOverlappedResult(handle, ctypes.byref(overlapped), ctypes.byref(transferred), True)
            raise RuntimeError('The private listener environment handoff timed out')
        checked(kernel.GetOverlappedResult(handle, ctypes.byref(overlapped), ctypes.byref(transferred), False))
        if operation == 'write' and isinstance(data, bytes) and transferred.value != len(data):
            raise RuntimeError('The private listener environment handoff was incomplete')
        return buffer.raw[:transferred.value]
    finally:
        kernel.CloseHandle(overlapped.event)


class EnvironmentPipe:
    def __init__(self) -> None:
        kernel, security = api()
        self.name = '\\\\.\\pipe\\agent-listener-' + secrets.token_hex(24)
        descriptor = wintypes.LPVOID()
        checked(security.ConvertStringSecurityDescriptorToSecurityDescriptorW(
            f'D:P(A;;GA;;;{current_user_sid()})', 1, ctypes.byref(descriptor), None))
        try:
            attributes = SecurityAttributes(ctypes.sizeof(SecurityAttributes), descriptor, False)
            self.handle = kernel.CreateNamedPipeW(self.name, 3 | 0x40000000 | 0x80000,
                                                   8, 1, 65536, 65536, 10000, ctypes.byref(attributes))
            if self.handle == wintypes.HANDLE(-1).value:
                raise ctypes.WinError(ctypes.get_last_error())
        finally:
            kernel.LocalFree(descriptor)

    def send(self, environment: dict[str, str], timeout: float = 10) -> None:
        deadline = time.monotonic() + timeout
        payload = json.dumps(environment, ensure_ascii=False).encode('utf-8')
        if len(payload) > 16 * 1024 * 1024:
            raise RuntimeError('The listener command environment exceeds its transfer limit')
        overlapped_io(self.handle, 'connect', None, deadline)
        overlapped_io(self.handle, 'write', len(payload).to_bytes(4, 'big') + payload, deadline)
        if overlapped_io(self.handle, 'read', 1, deadline) != b'1':
            raise RuntimeError('The private listener environment was not acknowledged')

    def __enter__(self) -> EnvironmentPipe:
        return self

    def __exit__(self, *_args: Any) -> None:
        api()[0].CloseHandle(self.handle)


def receive_environment(name: str, timeout: float = 10) -> dict[str, str]:
    if re.fullmatch(r'\\\\\.\\pipe\\agent-listener-[0-9a-f]{48}', name) is None:
        raise RuntimeError('A local private listener environment endpoint is required')
    kernel, _security = api()
    handle = kernel.CreateFileW(name, 0x80000000 | 2, 0, None, 3, 0x40000000, None)
    if handle == wintypes.HANDLE(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    deadline = time.monotonic() + timeout

    def read_exact(length: int) -> bytes:
        parts: list[bytes] = []
        while length:
            part = overlapped_io(handle, 'read', min(length, 65536), deadline)
            if not part:
                raise RuntimeError('The private listener environment was truncated')
            parts.append(part)
            length -= len(part)
        return b''.join(parts)

    try:
        length = int.from_bytes(read_exact(4), 'big')
        if not 0 < length <= 16 * 1024 * 1024:
            raise RuntimeError('The private listener environment length is invalid')
        value = json.loads(read_exact(length).decode('utf-8'))
        if (not isinstance(value, dict)
                or any(not isinstance(key, str) or not isinstance(item, str) or '\0' in key or '\0' in item
                       for key, item in value.items())):
            raise RuntimeError('The private listener environment is invalid')
        overlapped_io(handle, 'write', b'1', deadline)
        return value
    finally:
        kernel.CloseHandle(handle)
