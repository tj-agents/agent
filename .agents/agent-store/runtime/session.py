"""An agent session's lifecycle, turn state and work-item claim (Python 3.9+)."""

from dataclasses import dataclass
import time
from typing import Optional


PROMPT_TITLE_LENGTH = 80
OWNER = "owner"
HANDED_OFF = "handed_off"
WORKING = "working"
AWAITING_INPUT = "awaiting_input"


def session_handle(host, session_id):
    return f"{host}:{session_id}"


def prompt_title(prompt):
    if not isinstance(prompt, str):
        return None
    text = " ".join(prompt.split())
    if not text or text.startswith(("/", "<")):
        return None
    if len(text) <= PROMPT_TITLE_LENGTH:
        return text
    cut = text[:PROMPT_TITLE_LENGTH].rsplit(" ", 1)[0]
    return f"{cut}…"


def _kept(value, current):
    return current if value is None else value


@dataclass(frozen=True)
class ClientRegistration:
    title: Optional[str] = None
    pid: Optional[int] = None
    pid_started_at: Optional[float] = None

    @classmethod
    def from_entry(cls, entry):
        return cls(entry.get("title"), entry.get("pid"), entry.get("pid_started_at"))


@dataclass(eq=False)
class Session:
    handle: str
    host: str
    session_id: str
    machine: Optional[str] = None
    profile: Optional[str] = None
    title: Optional[str] = None
    cwd: Optional[str] = None
    repo: Optional[str] = None
    branch: Optional[str] = None
    head: Optional[str] = None
    work_item: Optional[str] = None
    role: Optional[str] = None
    pid: Optional[int] = None
    pid_started_at: Optional[float] = None
    started_at: Optional[float] = None
    last_activity_at: Optional[float] = None
    turn_state: Optional[str] = None
    ended_at: Optional[float] = None
    end_reason: Optional[str] = None
    predecessor: Optional[str] = None
    successor: Optional[str] = None

    @classmethod
    def start(cls, host, session_id, machine, profile, cwd=None, checkout=None, title=None, pid=None,
              pid_started_at=None):
        session = cls(session_handle(host, session_id), host, session_id, title=title)
        session._begin(machine, profile, cwd, checkout, pid, pid_started_at)
        return session

    def resume(self, machine, profile, cwd=None, checkout=None, title=None, pid=None, pid_started_at=None):
        if self.end_reason == HANDED_OFF:
            self.role = None
        self.title = _kept(title, self.title)
        self._begin(machine, profile, cwd, checkout, pid, pid_started_at)
        self.ended_at = None
        self.end_reason = None

    def _begin(self, machine, profile, cwd, checkout, pid, pid_started_at):
        now = time.time()
        self.machine = machine
        self.profile = profile
        self.cwd = None if cwd is None else str(cwd)
        self.repo = checkout.repo if checkout else None
        self.branch = checkout.branch if checkout else None
        self.head = checkout.head if checkout else None
        self.pid = pid
        self.pid_started_at = pid_started_at
        self.started_at = now
        self.last_activity_at = now
        self.turn_state = WORKING

    @property
    def ended(self):
        return self.ended_at is not None

    @property
    def is_owner(self):
        return self.role == OWNER

    @property
    def is_live_owner(self):
        return self.is_owner and not self.ended

    @property
    def claimed(self):
        return self.role is not None

    @property
    def has_client_identity(self):
        return self.pid is not None and self.pid_started_at is not None

    def attach_client(self, pid, pid_started_at, cwd):
        if self.ended:
            return
        self.pid = pid
        self.pid_started_at = pid_started_at
        self.cwd = str(cwd)

    def prompted(self, prompt=None):
        self.turn_state = WORKING
        self.last_activity_at = time.time()
        self.title = _kept(self.title, prompt_title(prompt))

    def stopped(self, checkout=None):
        self.turn_state = AWAITING_INPUT
        self.last_activity_at = time.time()
        if checkout is None:
            return
        self.cwd = _kept(checkout.root, self.cwd)
        self.repo = _kept(checkout.repo, self.repo)
        self.branch = _kept(checkout.branch, self.branch)
        self.head = _kept(checkout.head, self.head)

    def end(self, reason, successor=None):
        self.ended_at = _kept(self.ended_at, time.time())
        self.end_reason = _kept(self.end_reason, reason)
        self.successor = _kept(self.successor, successor)

    def reopen(self, reason):
        if reason is None or self.end_reason != reason:
            return False
        self.ended_at = None
        self.end_reason = None
        return True

    def ensure_current(self):
        if self.ended:
            raise ValueError(f"{self.handle} is not a current native session")

    def ensure_may_claim(self, key, require_existing_binding=False):
        if require_existing_binding and self.work_item not in (None, key):
            raise ValueError(
                f"{self.handle} is attached to {self.work_item}; bind the remote reference explicitly before claiming"
            )

    def claim(self, key, role, predecessor=None):
        self.role = role
        self.work_item = key
        self.predecessor = _kept(predecessor, self.predecessor)

    def hand_off(self, successor, named_predecessor):
        if named_predecessor != self.handle:
            raise ValueError(f"{self.handle} already owns this work item; hand off before taking ownership")
        self.end(HANDED_OFF, successor=successor)

    def demote(self):
        self.role = None

    @property
    def unattached(self):
        return self.work_item is None

    def attach_work_item(self, key):
        if not self.unattached:
            return False
        self.work_item = key
        return True

    def transfer_work_item(self, key):
        self.work_item = key

    def __eq__(self, other):
        if not isinstance(other, Session):
            return NotImplemented
        return self.handle == other.handle

    def __hash__(self):
        return hash(self.handle)
