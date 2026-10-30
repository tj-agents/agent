"""Work items, their identities and their remote tracker references (Python 3.9+)."""

from dataclasses import dataclass
import re
import time
from typing import Optional
import uuid


ACTIVE = "active"
DONE = "done"

_GITHUB_SCOPE = re.compile(r"github\.com/[a-z0-9][a-z0-9_.-]*/[a-z0-9_.-]+$")
_AZURE_SCOPE = re.compile(r"dev\.azure\.com/[a-z0-9][a-z0-9-]*$")
_NATIVE_ID = re.compile(r"[1-9][0-9]*$")
_EXECUTION_KEY = re.compile(r"execution:[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


def work_item_key(repo, feature, item):
    return f"{repo}:{feature}/{item}"


def execution_key():
    return f"execution:{uuid.uuid4()}"


def validate_execution_key(value):
    if not isinstance(value, str) or not _EXECUTION_KEY.fullmatch(value):
        raise ValueError("execution key must be execution:<lowercase UUID>")
    return value


def normalise_work_item_reference(provider, scope, native_id):
    if not all(isinstance(value, str) and value.strip() for value in (provider, scope, native_id)):
        raise ValueError("provider, scope and native id are required")
    provider = provider.strip().casefold()
    scope = scope.strip().casefold()
    native_id = native_id.strip()
    if provider == "github":
        if not _GITHUB_SCOPE.fullmatch(scope):
            raise ValueError("GitHub scope must be github.com/<lowercase-owner>/<lowercase-repository>")
    elif provider == "azure-devops":
        if not _AZURE_SCOPE.fullmatch(scope):
            raise ValueError("Azure DevOps scope must be dev.azure.com/<lowercase-organization>")
    else:
        raise ValueError("provider must be github or azure-devops")
    if not _NATIVE_ID.fullmatch(native_id):
        raise ValueError("native id must be a positive canonical decimal number")
    return provider, scope, native_id


@dataclass(eq=False)
class WorkItem:
    key: str
    repo: str
    feature: Optional[str] = None
    item: Optional[str] = None
    status: str = ACTIVE
    branch: Optional[str] = None
    pr: Optional[str] = None
    title: Optional[str] = None
    plan_path: Optional[str] = None
    updated_at: Optional[float] = None

    @staticmethod
    def require_identity(key, feature, item):
        if (feature is None) != (item is None):
            raise ValueError("work item feature and item must both be present or both be absent")
        if feature is None:
            validate_execution_key(key)

    @classmethod
    def create(cls, key, repo, feature=None, item=None, status=None, branch=None, pr=None, title=None,
               plan_path=None):
        cls.require_identity(key, feature, item)
        return cls(key, repo, feature, item, ACTIVE if status is None else status, branch, pr, title, plan_path,
                   time.time())

    @classmethod
    def for_branch(cls, repo, branch, title=None):
        return cls.create(execution_key(), repo, status=ACTIVE, branch=branch, title=title)

    @property
    def label(self):
        return f"{self.feature}/{self.item}" if self.feature is not None else self.key

    @property
    def is_open(self):
        return self.status != DONE

    @property
    def is_named(self):
        return self.feature is not None

    @property
    def absorbs_branch_items(self):
        return self.is_named and self.is_open and bool(self.branch)

    def can_be_absorbed(self, referenced, claimed):
        return not self.is_named and self.is_open and not referenced and not claimed

    def revise(self, status=None, branch=None, pr=None, title=None, plan_path=None):
        if status is not None:
            self.status = status
        if pr is not None:
            self.pr = pr
        if title is not None:
            self.title = title
        if plan_path is not None:
            self.plan_path = plan_path
        self.record_branch(branch)

    def record_branch(self, branch):
        if self.branch is None:
            self.branch = branch
        self.updated_at = time.time()

    def __eq__(self, other):
        if not isinstance(other, WorkItem):
            return NotImplemented
        return self.key == other.key

    def __hash__(self):
        return hash(self.key)


@dataclass(frozen=True)
class WorkItemReference:
    provider: str
    scope: str
    native_id: str
    work_item: str

    @classmethod
    def bind(cls, key, provider, scope, native_id):
        return cls(*normalise_work_item_reference(provider, scope, native_id), key)

    def rebind(self, key):
        if self.work_item == key:
            return False
        raise ValueError(
            f"{self.provider}/{self.scope}/{self.native_id} is already bound to {self.work_item}, not {key}"
        )
