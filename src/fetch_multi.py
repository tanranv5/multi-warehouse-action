#!/usr/bin/env python3
"""多仓静态路由生成器（两层结构：storeHouse -> urls)."""
from __future__ import annotations

import argparse
import json
import re
import time
import unicodedata
from copy import deepcopy
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

import requests
import yaml


class RouteBuilder:
    def __init__(
        self,
        config: Dict[str, Any],
        repo_root: Path,
        public_repo: str,
        public_branch: str,
    ) -> None:
        self.config = config
        self.repo_root = repo_root
        self.public_repo = public_repo
        self.public_branch = public_branch

        self.defaults = config.get("defaults", {})
        self.filters = config.get("filters", {})
        self.domestic = config.get("domestic", {})
        self.pipelines = config.get("pipelines", [])

        self.context: Dict[str, Any] = {}
        self.pipeline_records: List[Dict[str, Any]] = []
        self.artifacts: List[Dict[str, Any]] = []

    # ------------------------------------------------------------------
    def run(self) -> None:
        if not self.pipelines:
            raise ValueError("config/routes.yaml 未配置 pipelines")

        for pipeline in self.pipelines:
            pipeline_id = pipeline["id"]
            kind = pipeline["kind"]
            start = time.perf_counter()
            error_message: Optional[str] = None
            try:
                data = self._dispatch_pipeline(pipeline)
            except Exception as exc:  # noqa: BLE001
                error_message = str(exc)
                print(f"[WARN] pipeline {pipeline_id} failed: {exc}")
                data = self._default_payload_for_kind(kind)
            duration_ms = round((time.perf_counter() - start) * 1000, 2)

            self.context[pipeline_id] = data

            if pipeline.get("expand") and pipeline["kind"] != "local_urls_storehouse":
                entries = data.get("storeHouse", [])
                self._expand_storehouse_routes(
                    entries=entries,
                    expand_cfg=pipeline["expand"],
                    pipeline_id=pipeline_id,
                    origin=pipeline.get("origin", pipeline_id),
                )
                priority = self.config.get("warehouse_priority")
                if priority == pipeline_id and entries:
                    entries.insert(0, entries.pop())

            output_rel = pipeline.get("output")
            if output_rel:
                self._write_json(output_rel, data)
                self._register_artifact(
                    artifact_id=pipeline_id,
                    rel_path=output_rel,
                    artifact_type="pipeline",
                )

            record = {
                "id": pipeline_id,
                "kind": kind,
                "duration_ms": duration_ms,
                "output": output_rel,
                "source": pipeline.get("source"),
                "inputs": pipeline.get("inputs"),
            }
            if error_message:
                record["error"] = error_message
            self.pipeline_records.append(record)

        self._write_summary()
        self._write_domestic_links()

    # ------------------------------------------------------------------
    def _dispatch_pipeline(self, pipeline: Dict[str, Any]) -> Any:
        kind = pipeline["kind"]
        if kind == "remote_storehouse":
            return self._run_remote_storehouse(pipeline)
        if kind == "local_storehouse":
            return self._run_local_storehouse(pipeline)
        if kind == "local_urls_storehouse":
            return self._run_local_urls_storehouse(pipeline)
        if kind == "merge_storehouse":
            return self._run_merge_storehouse(pipeline)
        if kind == "copy_route":
            return self._run_copy_route(pipeline)
        raise ValueError(f"未实现的 pipeline kind: {kind}")

    def _run_remote_storehouse(self, pipeline: Dict[str, Any]) -> Dict[str, Any]:
        url = pipeline.get("source", {}).get("url")
        raw = self._fetch_json(url)
        entries = raw.get("storeHouse", [])
        clean = self._sanitize_storehouse(entries, pipeline.get("origin", pipeline["id"]))
        return {"storeHouse": clean}

    def _run_local_storehouse(self, pipeline: Dict[str, Any]) -> Dict[str, Any]:
        path = self.repo_root / pipeline.get("source", {}).get("path", "")
        field = pipeline.get("source", {}).get("field", "storeHouse")
        with path.open("r", encoding="utf-8") as fp:
            payload = json.load(fp)
        entries = payload.get(field, payload)
        if isinstance(entries, dict):
            entries = entries.get("storeHouse") or entries.get(field) or []
        if isinstance(entries, list) and entries and isinstance(entries[0], str):
            entries = [{"name": origin, "url": item} for item in entries]
        clean = self._sanitize_storehouse(entries, pipeline.get("origin", pipeline["id"]))
        return {"storeHouse": clean}

    def _run_local_urls_storehouse(self, pipeline: Dict[str, Any]) -> Dict[str, Any]:
        source = pipeline.get("source", {})
        store = pipeline.get("store", {})
        path = self.repo_root / source.get("path", "")
        field = source.get("field", "urls")
        with path.open("r", encoding="utf-8") as fp:
            payload = json.load(fp)
        entries = payload.get(field, payload)
        clean_urls = self._sanitize_urls(entries, store.get("name", pipeline["id"]))
        expand_cfg = pipeline.get("expand")
        if not expand_cfg:
            raise ValueError("local_urls_storehouse 需要 expand 配置")
        store_name = store.get("name", "本地多仓")
        store_remark = store.get("remark", pipeline.get("origin", pipeline["id"]))
        store_slug = self._slugify(store_name)
        rel_path = self._write_storehouse_urls(store_slug, clean_urls, expand_cfg)
        public_urls = self._build_public_urls(rel_path, expand_cfg.get("level2_public_templates"))
        entry = {
            "sourceName": store_name,
            "sourceUrl": public_urls[0],
            "sourceRemark": store_remark,
        }
        self._register_artifact(
            artifact_id=f"{pipeline['id']}::{store_slug}",
            rel_path=rel_path,
            artifact_type="storehouse",
            metadata={"origin": pipeline.get("origin"), "store": store_name},
            templates=expand_cfg.get("level2_public_templates"),
        )
        return {"storeHouse": [entry]}

    def _run_merge_storehouse(self, pipeline: Dict[str, Any]) -> Dict[str, Any]:
        merged: List[Dict[str, Any]] = []
        for ref in pipeline.get("inputs", []):
            data = self.context.get(ref)
            if not data:
                raise ValueError(f"依赖 pipeline {ref} 未生成数据")
            merged.extend(deepcopy(data.get("storeHouse", [])))

        deduped: List[Dict[str, Any]] = []
        seen = set()
        for entry in merged:
            url = entry.get("sourceUrl")
            if not url or url in seen:
                continue
            seen.add(url)
            deduped.append(entry)
        return {"storeHouse": deduped}

    def _run_copy_route(self, pipeline: Dict[str, Any]) -> Any:
        ref = pipeline.get("input")
        data = self.context.get(ref)
        if data is None:
            raise ValueError(f"copy_route 无法找到输入 {ref}")
        return deepcopy(data)

    # ------------------------------------------------------------------
    def _expand_storehouse_routes(
        self,
        entries: List[Dict[str, Any]],
        expand_cfg: Dict[str, Any],
        pipeline_id: str,
        origin: str,
    ) -> None:
        field = expand_cfg.get("level2_field", "urls")
        for entry in entries:
            original_url = entry.get("sourceUrl")
            if not original_url:
                continue
            try:
                raw_urls = self._fetch_level2_urls(original_url, field)
            except Exception as exc:  # noqa: BLE001
                print(f"[WARN] 无法读取 {original_url}: {exc}")
                continue
            store_name = entry.get("sourceName") or origin
            store_slug = self._slugify(store_name)
            clean_urls = self._sanitize_urls(raw_urls, store_name)
            rel_path = self._write_storehouse_urls(store_slug, clean_urls, expand_cfg)
            public_urls = self._build_public_urls(rel_path, expand_cfg.get("level2_public_templates"))
            entry["sourceUrl"] = public_urls[0]
            entry.setdefault("sourceRemark", origin)
            self._register_artifact(
                artifact_id=f"{pipeline_id}::{store_slug}",
                rel_path=rel_path,
                artifact_type="storehouse",
                metadata={"original_level2": original_url, "store": store_name},
                templates=expand_cfg.get("level2_public_templates"),
            )

    def _write_storehouse_urls(
        self,
        store_slug: str,
        urls: List[Dict[str, Any]],
        expand_cfg: Dict[str, Any],
    ) -> str:
        level2_dir = Path(expand_cfg.get("level2_output_dir", "dist/routes/storehouses"))
        level2_path = self.repo_root / level2_dir
        level2_path.mkdir(parents=True, exist_ok=True)
        rel_path = level2_dir / f"{store_slug}.json"
        self._write_json(rel_path, {"urls": urls})
        return str(rel_path)

    # ------------------------------------------------------------------
    def _fetch_json(self, url: str) -> Dict[str, Any]:
        headers = self.defaults.get("headers", {})
        timeout = self.defaults.get("timeout", 10)
        attempts = self.defaults.get("retries", 3)
        last_exc: Optional[Exception] = None
        for attempt in range(1, attempts + 1):
            try:
                response = requests.get(url, headers=headers, timeout=timeout)
                response.raise_for_status()
                return response.json()
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                if attempt < attempts:
                    time.sleep(min(2 ** attempt, 5))
                else:
                    raise

    def _fetch_level2_urls(self, url: str, field: str) -> List[Dict[str, Any]]:
        headers = self.defaults.get("headers", {})
        timeout = self.defaults.get("timeout", 10)
        response = requests.get(url, headers=headers, timeout=timeout)
        response.raise_for_status()
        payload = response.json()
        entries = payload.get(field, payload)
        if not isinstance(entries, list):
            raise ValueError("level2 响应不是数组")
        return entries

    def _sanitize_storehouse(self, entries: List[Dict[str, Any]], origin: str) -> List[Dict[str, Any]]:
        clean: List[Dict[str, Any]] = []
        blocked_keywords = [str(k).lower() for k in self.filters.get("blocked_keywords", [])]
        blocked_patterns = [str(k).lower() for k in self.filters.get("blocked_url_keywords", [])]
        blocked_domains = [str(k).lower() for k in self.filters.get("blocked_domains", [])]

        for entry in entries:
            name = (entry.get("sourceName") or entry.get("name") or "").strip()
            url = (entry.get("sourceUrl") or entry.get("url") or "").strip()
            remark = entry.get("sourceRemark") or entry.get("remark")
            if not name or not url:
                continue
            if self._is_blocked(name, url, blocked_keywords, blocked_patterns, blocked_domains):
                continue
            clean.append(
                {
                    "sourceName": name,
                    "sourceUrl": url,
                    "sourceRemark": remark or origin,
                }
            )
        return clean

    def _sanitize_urls(self, entries: List[Dict[str, Any]], origin: str) -> List[Dict[str, Any]]:
        clean: List[Dict[str, Any]] = []
        blocked_keywords = [str(k).lower() for k in self.filters.get("blocked_keywords", [])]
        blocked_patterns = [str(k).lower() for k in self.filters.get("blocked_url_keywords", [])]
        blocked_domains = [str(k).lower() for k in self.filters.get("blocked_domains", [])]

        for entry in entries:
            name = (entry.get("name") or entry.get("sourceName") or origin).strip()
            url = (entry.get("url") or entry.get("sourceUrl") or "").strip()
            if not url:
                continue
            if self._is_blocked(name, url, blocked_keywords, blocked_patterns, blocked_domains):
                continue
            clean.append({"name": name, "url": url})
        return clean

    def _is_blocked(
        self,
        name: str,
        url: str,
        blocked_keywords: List[str],
        blocked_patterns: List[str],
        blocked_domains: List[str],
    ) -> bool:
        lower_name = name.lower()
        lower_url = url.lower()
        if any(keyword in lower_name for keyword in blocked_keywords):
            return True
        if any(pattern in lower_url for pattern in blocked_patterns):
            return True
        if blocked_domains:
            domain = urlparse(url).netloc.lower()
            if any(domain.endswith(blocked) for blocked in blocked_domains):
                return True
        return False

    # ------------------------------------------------------------------
    def _write_summary(self) -> None:
        summary = {
            "generated_at": time.time(),
            "pipelines": self.pipeline_records,
            "cdn_index": self._build_public_urls(
                "dist/routes/multi/index.json",
                ["https://cdn.jsdelivr.net/gh/{repo}@{branch}/{path}"],
            )[0],
        }
        self._write_json("dist/meta/routes_summary.json", summary)

    def _write_domestic_links(self) -> None:
        templates = self.domestic.get("templates") or [
            "https://raw.githubusercontent.com/{repo}/{branch}/{path}",
        ]
        payload = []
        for artifact in self.artifacts:
            rel_path = artifact["path"]
            mirrors = [
                template.format(repo=self.public_repo, branch=self.public_branch, path=rel_path)
                for template in templates
            ]
            payload.append(
                {
                    "id": artifact["id"],
                    "type": artifact["type"],
                    "path": rel_path,
                    "mirrors": mirrors,
                    "metadata": artifact.get("metadata"),
                }
            )
        self._write_json("dist/meta/domestic_links.json", payload)

    def _write_json(self, rel_path: str, data: Any) -> None:
        path = self.repo_root / rel_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    def _register_artifact(
        self,
        artifact_id: str,
        rel_path: str,
        artifact_type: str,
        metadata: Optional[Dict[str, Any]] = None,
        templates: Optional[List[str]] = None,
    ) -> None:
        self.artifacts.append(
            {
                "id": artifact_id,
                "path": rel_path,
                "type": artifact_type,
                "metadata": metadata or {},
                "templates": templates,
            }
        )

    def _build_public_urls(self, rel_path: str, templates: Optional[List[str]] = None) -> List[str]:
        tpl = templates or self.domestic.get("templates") or [
            "https://raw.githubusercontent.com/{repo}/{branch}/{path}",
        ]
        return [
            template.format(repo=self.public_repo, branch=self.public_branch, path=rel_path)
            for template in tpl
        ]

    def _slugify(self, value: str) -> str:
        normalized = unicodedata.normalize("NFKD", value)
        ascii_value = normalized.encode("ascii", "ignore").decode("ascii")
        ascii_value = re.sub(r"[^a-zA-Z0-9]+", "-", ascii_value).strip("-")
        return ascii_value.lower() or "source"

    def _default_payload_for_kind(self, kind: str) -> Dict[str, Any]:
        if kind in {"remote_storehouse", "local_storehouse", "local_urls_storehouse", "merge_storehouse", "copy_route"}:
            return {"storeHouse": []}
        if kind == "remote_urls":
            return {"urls": []}
        return {}


def refresh_qingning_sources(config: Dict[str, Any], repo_root: Path) -> None:
    """从 QingNing 仓库 README 自动同步单仓列表."""
    settings = config.get("qingning_remote")
    if not settings or settings.get("enabled", True) is False:
        return

    urls = settings.get("urls") or [settings.get("url")]
    timeout = settings.get("timeout", 15)
    headers = settings.get("headers") or {}
    text: Optional[str] = None
    for url in urls:
        if not url:
            continue
        try:
            response = requests.get(url, timeout=timeout, headers=headers)
            response.raise_for_status()
            response.encoding = response.encoding or "utf-8"
            text = response.text
            break
        except Exception as exc:  # noqa: BLE001
            print(f"[WARN] QingNing 源 {url} 拉取失败: {exc}")
    if text is None:
        print("[WARN] QingNing README 未能获取，保留现有 data/qingning_single.json")
        return

    sources: List[Dict[str, str]] = []
    seen_urls = set()
    current_name: Optional[str] = None
    name_template = settings.get("single_name_template", "🌟{name}🌟")
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if "【单仓】" in line:
            name_match = re.search(r"【单仓】\s*([^：:】]+)", line)
            if name_match:
                current_name = name_match.group(1).strip()
            url_match = re.search(r"(https?://[^\s*]+)", line)
            if url_match:
                url = url_match.group(1).strip().rstrip(")")
                raw_name = current_name or "青宁线路"
                clean_name = re.sub(r"^[\s【】]*(单仓)?", "", raw_name).strip() or raw_name
                name = name_template.format(name=clean_name)
                if url and url not in seen_urls:
                    seen_urls.add(url)
                    sources.append({"name": name, "url": url})
                current_name = None
            continue

        url_match = re.search(r"(https?://[^\s*]+)", line)
        if url_match and current_name:
            url = url_match.group(1).strip().rstrip(")")
            if url and url not in seen_urls:
                seen_urls.add(url)
                clean_name = re.sub(r"^[\s【】]*(单仓)?", "", current_name).strip() or current_name
                name = name_template.format(name=clean_name)
                sources.append({"name": name, "url": url})
            current_name = None

    if not sources:
        print("[WARN] QingNing README 未解析出单仓链接，保留旧数据")
        return

    sources = validate_single_sources(sources, settings.get("validation"), headers)
    if not sources:
        print("[WARN] QingNing 验证后无可用单仓，保留旧数据")
        return

    remark_template = settings.get("remark_template", "青宁自动抓取：{name}")
    payload = {
        "sources": [
            {
                "name": item["name"],
                "url": item["url"],
                "remark": remark_template.format(name=item["name"], url=item["url"]),
            }
            for item in sources
        ]
    }
    output_path = repo_root / settings.get("output", "data/qingning_single.json")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[INFO] 已同步 QingNing 单仓 {len(sources)} 条 -> {output_path}")


def validate_single_sources(
    sources: List[Dict[str, str]],
    validation_settings: Optional[Dict[str, Any]],
    headers_override: Optional[Dict[str, str]] = None,
) -> List[Dict[str, str]]:
    if not validation_settings or validation_settings.get("enabled", True) is False:
        return sources
    timeout = validation_settings.get("timeout", 10)
    require_json = validation_settings.get("require_json", False)
    headers = headers_override or validation_settings.get("headers") or {}
    max_count = validation_settings.get("max_count")

    validated: List[Dict[str, str]] = []
    for item in sources:
        if max_count and len(validated) >= max_count:
            break
        url = item["url"]
        try:
            resp = requests.get(url, headers=headers, timeout=timeout)
            resp.raise_for_status()
            if require_json:
                json.loads(resp.text)
            validated.append(item)
        except Exception as exc:  # noqa: BLE001
            print(f"[WARN] 跳过不可用单仓 {url}: {exc}")
    return validated


# ----------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="多仓静态路由生成工具")
    parser.add_argument("--config", default="config/routes.yaml", help="配置文件路径")
    parser.add_argument(
        "--public-repo",
        default="your-user/multi-warehouse-action",
        help="用于国内加速地址生成的 repo（格式：owner/repo）",
    )
    parser.add_argument(
        "--public-branch",
        default="main",
        help="生成国内加速地址时使用的分支",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    repo_root = Path.cwd()
    with (repo_root / args.config).open("r", encoding="utf-8") as fp:
        config = yaml.safe_load(fp)
    refresh_qingning_sources(config, repo_root)
    builder = RouteBuilder(
        config=config,
        repo_root=repo_root,
        public_repo=args.public_repo,
        public_branch=args.public_branch,
    )
    builder.run()


if __name__ == "__main__":
    main()
