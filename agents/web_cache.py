"""Session-local cache for successful, read-only web calls."""
import asyncio
from collections import OrderedDict
from dataclasses import replace
import sys
from time import monotonic
from weakref import WeakValueDictionary

from .tool_result import ToolExecutionResult
from .web_tools import search_provider


class WebCache:
    def __init__(self, ttl: float = 1800):
        self.ttl = ttl
        self.entries = OrderedDict()
        self.locks = WeakValueDictionary()
        self.size = 0

    async def execute(self, name, args, dispatch) -> ToolExecutionResult:
        provider = search_provider().name if name == "web_search" else None
        key = (name, provider, tuple(sorted(args.items())))
        lock = self.locks.setdefault(key, asyncio.Lock())
        async with lock:
            entry = self.entries.get(key)
            if entry is not None:
                deadline, result = entry
                if deadline > monotonic():
                    self.entries.move_to_end(key)
                    return replace(result, meta={**result.meta, "cache_hit": True})
                self.size -= sys.getsizeof(result.content)
                del self.entries[key]
            raw = await dispatch(name, args)
            result = raw if isinstance(raw, ToolExecutionResult) else ToolExecutionResult(raw)
            size = sys.getsizeof(result.content)
            budget = 16 * 1024 * 1024
            if result.ok and size <= budget:
                while self.entries and (len(self.entries) >= 128 or self.size + size > budget):
                    _, (_, old) = self.entries.popitem(last=False)
                    self.size -= sys.getsizeof(old.content)
                self.entries[key] = (monotonic() + self.ttl, result)
                self.size += size
            return result
