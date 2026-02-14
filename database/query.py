from typing import TypeVar, Generic
from pydantic import BaseModel

T = TypeVar("T", bound=BaseModel)


class SelectQuery(Generic[T]):
    def __init__(self, model: type[T], sql: str, *args):
        self.sql = sql
        self.args = args
        self.model = model


class ExecutableQuery:
    def __init__(self, sql: str, *args):
        self.sql = sql
        self.args = args
