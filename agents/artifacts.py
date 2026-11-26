"""Session-scoped search candidates and their latest screening decisions."""
from contextlib import contextmanager
from datetime import datetime, timezone
import json
from pathlib import Path
import re
from tempfile import NamedTemporaryFile
from urllib.parse import unquote, unquote_plus, urlsplit, urlunsplit

from .event_log import _lock_for_append
from .paths import RESEARCH_DIRNAME
from .session import validate_session_id

DECISIONS = ("candidate", "selected", "rejected")
STATUSES = ("seen", *DECISIONS)
_ARXIV = r"(?:\d{4}\.\d{4,5}|[a-z][a-z0-9.-]*/\d{7})"
_DOI = r"10\.\d{4,9}/[^\s?#]+"
_TRACKING = {"fbclid", "gclid", "msclkid", "mc_cid", "mc_eid"}


def canonical_id(url: str) -> str:
    parts = urlsplit(url.strip())
    host = parts.hostname
    if parts.scheme not in ("http", "https") or not host or parts.username is not None:
        raise ValueError("Expected an HTTP(S) result URL without credentials.")
    path = unquote(parts.path).rstrip("/")
    if host in {"arxiv.org", "www.arxiv.org", "export.arxiv.org"}:
        match = re.fullmatch(rf"/(?:abs|pdf|html)/({_ARXIV})(?:v\d+)?(?:\.pdf)?", path, re.I)
        if match:
            return "arxiv:" + match[1].lower()

    doi = re.search(rf"/({_DOI})$", path, re.I)
    if doi:
        value = doi[1].lower()
        arxiv = re.fullmatch(rf"10\.48550/arxiv\.({_ARXIV})(?:v\d+)?", value)
        return "arxiv:" + arxiv[1] if arxiv else "doi:" + value

    authority = f"[{host}]" if ":" in host else host
    if parts.port is not None and (parts.scheme, parts.port) not in {("https", 443), ("http", 80)}:
        authority += f":{parts.port}"
    query = []
    for field in parts.query.split("&"):
        key = unquote_plus(field.partition("=")[0]).lower()
        if field and not key.startswith("utm_") and key not in _TRACKING:
            query.append(field)
    return "url:" + urlunsplit((parts.scheme, authority, parts.path.rstrip("/"), "&".join(query), ""))


def candidate_label(record: dict, known: bool) -> str:
    label = "历史已记录" if known else "首次记录"
    reason = f" | 原因: {record['reason']}" if record["reason"] else ""
    return f"[{label} | {record['canonical_id']} | 状态: {record['status']}{reason}]"


class CandidateLedger:
    def __init__(self, cwd: Path, session_id: str):
        validate_session_id(session_id)
        self.path = cwd / RESEARCH_DIRNAME / "artifacts" / session_id / "candidates.jsonl"

    def _read(self) -> dict:
        try:
            stream = self.path.open(encoding="utf-8")
        except FileNotFoundError:
            return {}
        with stream:
            rows = (json.loads(line) for line in stream)
            return {row["canonical_id"]: row for row in rows}

    @contextmanager
    def _write_lock(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # The data file is replaced atomically; lock a stable sibling inode.
        with self.path.with_suffix(".lock").open("a+b") as lock:
            _lock_for_append(lock)
            yield

    def _save(self, rows: dict) -> None:
        stream = NamedTemporaryFile(mode="w", encoding="utf-8", dir=self.path.parent, delete=False)
        temporary = Path(stream.name)
        try:
            with stream:
                for row in rows.values():
                    stream.write(json.dumps(row, ensure_ascii=False) + "\n")
            temporary.replace(self.path)
        finally:
            temporary.unlink(missing_ok=True)

    def observe(self, hits: list[dict]) -> list[tuple[dict, bool]]:
        """Register a provider batch and return the record and prior-presence flag for each hit."""
        observed = []
        if not hits:
            return observed
        with self._write_lock():
            rows = self._read()
            count = len(rows)
            now = datetime.now(timezone.utc).isoformat()
            for hit in hits:
                url = hit["url"]
                key = canonical_id(url)
                known = key in rows
                if not known:
                    source = urlsplit(url).hostname if key.startswith("url:") else key.partition(":")[0]
                    rows[key] = dict(canonical_id=key, url=url, title=hit["title"], source=source,
                                     status="seen", reason="", first_seen_at=now, updated_at=now)
                observed.append((rows[key], known))
            if len(rows) != count:
                self._save(rows)
        return observed

    def update(self, canonical_id: str, status: str, reason: str) -> dict:
        if status not in DECISIONS or not reason.strip():
            raise ValueError("Provide a candidate, selected or rejected status and a non-empty reason.")
        with self._write_lock():
            rows = self._read()
            if canonical_id not in rows:
                raise ValueError(f"Unknown candidate: {canonical_id}")
            record = rows[canonical_id]
            reason = reason.strip()
            if (record["status"], record["reason"]) != (status, reason):
                record.update(status=status, reason=reason, updated_at=datetime.now(timezone.utc).isoformat())
                self._save(rows)
            return record

    def list_candidates(self, status: str | None = None) -> list[dict]:
        if status is not None and status not in STATUSES:
            raise ValueError(f"Unknown candidate status: {status}")
        return [row for row in self._read().values() if status is None or row["status"] == status]
