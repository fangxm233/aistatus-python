from __future__ import annotations

import csv
import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


class UsageStorage:
    def __init__(self, base_dir: Path | None = None, cwd: str | None = None):
        self._base_dir = base_dir or (Path.home() / ".aistatus" / "usage")
        self._cwd = cwd or str(Path.cwd())
        self._project_dir = self._resolve_project_dir()
        self._project_dir.mkdir(parents=True, exist_ok=True)
        self._ensure_manifest()

    def append(self, record: dict[str, Any]) -> None:
        month_file = self._project_dir / f"{self._month_key(record.get('ts'))}.jsonl"
        month_file.parent.mkdir(parents=True, exist_ok=True)
        with month_file.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")

    def read(self, period: str = "month", all_projects: bool = False) -> list[dict[str, Any]]:
        project_dirs = self._all_project_dirs() if all_projects else [self._project_dir]
        since = self._period_since(period)
        records: list[dict[str, Any]] = []
        for project_dir in project_dirs:
            for file_path in sorted(project_dir.glob("*.jsonl")):
                records.extend(self._read_jsonl(file_path, since, project_dir))
        return records

    def list_projects(self) -> list[dict[str, Any]]:
        projects: list[dict[str, Any]] = []
        for project_dir in self._all_project_dirs():
            manifest_path = project_dir / "manifest.json"
            if not manifest_path.exists():
                continue
            try:
                data = json.loads(manifest_path.read_text(encoding="utf-8"))
            except Exception:
                continue
            data["id"] = project_dir.name
            projects.append(data)
        return sorted(projects, key=lambda item: item.get("path", ""))

    def export_csv(self, records: list[dict[str, Any]], output_path: str | Path) -> None:
        path = Path(output_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        fieldnames = ["ts", "project", "provider", "model", "in", "out", "cost", "fallback", "latency_ms"]
        with path.open("w", encoding="utf-8", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=fieldnames)
            writer.writeheader()
            for row in records:
                writer.writerow({key: row.get(key) for key in fieldnames})

    def export_json(self, payload: Any, output_path: str | Path) -> None:
        path = Path(output_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    def _resolve_project_dir(self) -> Path:
        cwd_hash = hashlib.sha256(self._cwd.encode("utf-8")).hexdigest()[:12]
        return self._base_dir / "projects" / cwd_hash

    def _ensure_manifest(self) -> None:
        manifest_path = self._project_dir / "manifest.json"
        if manifest_path.exists():
            return
        manifest = {
            "path": self._cwd,
            "created": datetime.now(timezone.utc).isoformat(),
        }
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

    def _all_project_dirs(self) -> list[Path]:
        root = self._base_dir / "projects"
        if not root.exists():
            return []
        return [path for path in root.iterdir() if path.is_dir()]

    def _read_jsonl(
        self,
        file_path: Path,
        since: datetime | None,
        project_dir: Path,
    ) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        for line in file_path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except Exception:
                continue
            ts = self._parse_ts(record.get("ts"))
            if since and (ts is None or ts < since):
                continue
            record.setdefault("project", project_dir.name)
            records.append(record)
        return records

    @staticmethod
    def _month_key(ts: Any) -> str:
        dt = UsageStorage._parse_ts(ts) or datetime.now(timezone.utc)
        return dt.strftime("%Y-%m")

    @staticmethod
    def _period_since(period: str) -> datetime | None:
        now = datetime.now(timezone.utc)
        if period == "week":
            return now - timedelta(days=7)
        if period == "month":
            return now - timedelta(days=30)
        if period == "all":
            return None
        raise ValueError(f"Unsupported period: {period}")

    @staticmethod
    def _parse_ts(value: Any) -> datetime | None:
        if not value or not isinstance(value, str):
            return None
        try:
            if value.endswith("Z"):
                value = value[:-1] + "+00:00"
            return datetime.fromisoformat(value)
        except ValueError:
            return None
