from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any, cast

logger = logging.getLogger(__name__)


class SourcePoller:
    """Generic multi-source poller for heartbeat integration."""

    def __init__(self, config: dict, executor: Any, mcp_registry: Any = None):
        """
        config: heartbeat configuration dict.
        executor: Executor instance for native tools.
        mcp_registry: MCPServerRegistry instance for MCP tools.
        """
        self.config = config
        self.sources = config.get("heartbeat", {}).get("sources", [])
        self.executor = executor
        self.mcp_registry = mcp_registry
        self.last_poll: dict[str, datetime] = {}  # source_name -> last poll time

    async def _poll_watch_topics(self, now: datetime) -> list[dict]:
        """
        For each watch topic in profile["watch_topics"], check if interval elapsed.
        If due, call the configured web search MCP server and return raw results.
        Does nothing if no web search server is configured or no watch_topics in profile.
        """
        import hashlib

        watch_topics = self.config.get("watch_topics", [])
        if not watch_topics:
            return []

        # Find web search server
        web_search_server_conf = next(
            (
                s
                for s in self.config.get("mcp_servers", [])
                if s.get("type") == "web_search" or any(kw in s.get("name", "").lower() for kw in ("brave", "tavily"))
            ),
            None,
        )

        if not web_search_server_conf:
            logger.debug("No web search MCP server configured for watch_topics.")
            return []

        if not self.mcp_registry:
            logger.debug("mcp_registry is not initialized for watch_topics.")
            return []

        server_name = web_search_server_conf["name"]
        tool_name = web_search_server_conf.get("tool", "search")
        client = self.mcp_registry.get_client(server_name)
        if not client:
            logger.warning(f"MCP client for '{server_name}' not found for watch_topics")
            return []

        results = []
        for topic in watch_topics:
            query = topic.get("query")
            if not query:
                continue

            interval_min = topic.get("interval_minutes", 60)
            interval = timedelta(minutes=interval_min)

            query_hash = hashlib.sha256(query.encode()).hexdigest()[:8]
            poll_key = f"watch:{query_hash}"
            last = self.last_poll.get(poll_key, datetime.min)

            if now - last < interval:
                continue

            max_results = topic.get("max_results", 5)
            if not (1 <= max_results <= 10):
                logger.warning(f"max_results {max_results} out of range [1, 10] for query '{query}'. Clamping.")
                max_results = max(1, min(max_results, 10))

            args = {
                "query": query,
                "count": max_results,
            }

            try:
                raw_mcp_result = client.call_tool(tool_name, args)
                self.last_poll[poll_key] = now
                results.append(
                    {
                        "source": f"web_search:{query[:30]}",
                        "type": "mcp",
                        "data": raw_mcp_result,
                        "extractor": "web_search",
                        "metadata": {"query": query},
                    }
                )
            except Exception as e:
                logger.error(f"Watch topic '{query}' poll failed: {e}", exc_info=True)

        return results

    async def _poll_watch_dirs(self, now: datetime) -> list[dict]:
        """
        For each watch directory in profile["watch_dirs"], check if interval elapsed.
        If due, call the configured filesystem MCP server and return raw results.
        Does nothing if no filesystem server is configured or no watch_dirs in profile.
        """
        import hashlib
        import os

        watch_dirs = self.config.get("watch_dirs", [])
        if not watch_dirs:
            return []

        # Find filesystem server
        fs_server_conf = next(
            (
                s
                for s in self.config.get("mcp_servers", [])
                if s.get("type") == "filesystem" or "filesystem" in s.get("name", "").lower()
            ),
            None,
        )

        if not fs_server_conf:
            logger.debug("No filesystem MCP server configured for watch_dirs.")
            return []

        if not self.mcp_registry:
            logger.debug("mcp_registry is not initialized for watch_dirs.")
            return []

        server_name = fs_server_conf["name"]
        list_tool = fs_server_conf.get("list_tool", "list_directory")
        read_tool = fs_server_conf.get("read_tool", "read_multiple_files")

        client = self.mcp_registry.get_client(server_name)
        if not client:
            logger.warning(f"MCP client for '{server_name}' not found for watch_dirs")
            return []

        results = []
        for dir_config in watch_dirs:
            path = dir_config.get("path")
            if not path:
                continue

            resolved_path = os.path.abspath(os.path.expanduser(path))
            interval_min = dir_config.get("interval_minutes", 60)
            interval = timedelta(minutes=interval_min)

            dir_hash = hashlib.sha256(resolved_path.encode()).hexdigest()[:8]
            poll_key = f"watchdir:{dir_hash}"
            last = self.last_poll.get(poll_key, datetime.min)

            if now - last < interval:
                continue

            try:
                # 1. List directory
                list_result = client.call_tool(list_tool, {"path": resolved_path})
                self.last_poll[poll_key] = now

                # Extract file list from content
                files_metadata = []
                structured = list_result.get("structured")
                if structured and "entries" in structured:
                    for entry in structured["entries"]:
                        if entry.get("type") == "file":
                            files_metadata.append({"name": entry["name"], "modifiedAt": entry.get("modifiedAt", "")})
                else:
                    content_list = list_result.get("content", [])
                    if not content_list:
                        logger.debug(f"Empty listing for {resolved_path}")
                        continue

                    for item in content_list:
                        if item.get("type") == "text" and "text" in item:
                            # list_directory returns newline-separated filenames
                            filenames = item["text"].splitlines()
                            for fname in filenames:
                                if fname.strip():
                                    files_metadata.append({"name": fname.strip(), "modifiedAt": ""})

                if not files_metadata:
                    continue

                # 2. Filter by extensions
                extensions = dir_config.get("extensions", [])
                if extensions:
                    extensions = [ext.lower().lstrip(".") for ext in extensions]
                    files_metadata = [
                        f for f in files_metadata if any(f["name"].lower().endswith(f".{ext}") for ext in extensions)
                    ]

                if not files_metadata:
                    continue

                # 3. Sort by modifiedAt descending (newest first) and take max_files
                files_metadata.sort(key=lambda x: x["modifiedAt"], reverse=True)

                max_files = dir_config.get("max_files", 10)
                if not (1 <= max_files <= 20):
                    logger.warning(f"max_files {max_files} out of range [1, 20] for {resolved_path}. Clamping.")
                    max_files = max(1, min(max_files, 20))

                files_to_read = [f["name"] for f in files_metadata[:max_files]]
                full_paths = [os.path.join(resolved_path, f) for f in files_to_read]

                # 4. Call read_multiple_files
                raw_mcp_result = client.call_tool(read_tool, {"paths": full_paths})

                results.append(
                    {
                        "source": f"filesystem:{os.path.basename(resolved_path)}",
                        "type": "mcp",
                        "data": raw_mcp_result,
                        "extractor": "file_content",
                        "metadata": {
                            "watch_dir": resolved_path,
                            "dir_config": dir_config,
                        },
                    }
                )
            except Exception as e:
                logger.error(f"Watch dir '{resolved_path}' poll failed: {e}", exc_info=True)

        return results

    async def poll_due_sources(self) -> list[dict]:
        """Poll all sources whose interval has elapsed. Returns raw results."""
        results = []
        now = datetime.utcnow()

        for source in self.sources:
            name = source["name"]
            interval = timedelta(minutes=source.get("interval_minutes", 15))
            last = self.last_poll.get(name, datetime.min)

            if now - last < interval:
                continue

            try:
                result = await self._poll_source(source)
                self.last_poll[name] = now
                results.append(
                    {
                        "source": name,
                        "type": source["type"],
                        "data": result,
                        "extractor": source.get("signal_extractor", "generic"),
                    }
                )
            except Exception as e:
                logger.error(f"Source '{name}' poll failed: {e}", exc_info=True)
                # Don't update last_poll — retry next tick
                results.append(
                    {
                        "source": name,
                        "type": source["type"],
                        "data": None,
                        "error": str(e),
                        "extractor": source.get("signal_extractor", "generic"),
                    }
                )

        watch_results = await self._poll_watch_topics(now)
        results.extend(watch_results)

        dir_results = await self._poll_watch_dirs(now)
        results.extend(dir_results)

        repo_results = await self._poll_watch_repos(now)
        results.extend(repo_results)

        return results

    async def _poll_watch_repos(self, now: datetime) -> list[dict]:
        """
        For each repo in profile["watch_repos"], check if interval elapsed.
        If due, call the configured GitHub MCP server for each enabled event type
        (commits, issues, pull requests) and return raw results.
        Does nothing if no GitHub server is configured or no watch_repos in profile.
        """
        import hashlib
        import os

        watch_repos = self.config.get("watch_repos", [])
        if not watch_repos:
            return []

        # Find GitHub server
        github_server_conf = next(
            (
                s
                for s in self.config.get("mcp_servers", [])
                if s.get("type") == "github" or "github" in s.get("name", "").lower()
            ),
            None,
        )

        if not github_server_conf:
            logger.debug("No GitHub MCP server configured for watch_repos.")
            return []

        if not self.mcp_registry:
            logger.debug("mcp_registry is not initialized for watch_repos.")
            return []

        if not os.environ.get("GITHUB_TOKEN"):
            logger.warning(
                "GITHUB_TOKEN not set — GitHub MCP source is configured but cannot authenticate. "
                "Set GITHUB_TOKEN in your environment to enable repository watching."
            )
            return []

        server_name = github_server_conf["name"]
        client = self.mcp_registry.get_client(server_name)
        if not client:
            logger.warning(f"MCP client for '{server_name}' not found for watch_repos")
            return []

        # Tool names from config
        commits_tool = github_server_conf.get("commits_tool", "list_commits")
        issues_tool = github_server_conf.get("issues_tool", "list_issues")
        prs_tool = github_server_conf.get("prs_tool", "list_pull_requests")

        results = []
        for repo_config in watch_repos:
            repo = repo_config.get("repo")
            if not repo:
                continue

            interval_min = repo_config.get("interval_minutes", 60)
            interval = timedelta(minutes=interval_min)
            max_items = repo_config.get("max_items", 10)
            if not (1 <= max_items <= 20):
                logger.warning(f"max_items {max_items} out of range [1, 20] for repo '{repo}'. Clamping.")
                max_items = max(1, min(max_items, 20))

            repo_hash = hashlib.sha256(repo.encode()).hexdigest()[:8]

            event_types = []
            if repo_config.get("watch_commits", True):
                event_types.append(("commits", commits_tool))
            if repo_config.get("watch_issues", False):
                event_types.append(("issues", issues_tool))
            if repo_config.get("watch_prs", True):
                event_types.append(("prs", prs_tool))

            for event_type, tool_name in event_types:
                poll_key = f"watchrepo:{repo_hash}:{event_type}"
                last = self.last_poll.get(poll_key, datetime.min)

                if now - last < interval:
                    continue

                try:
                    raw_mcp_result = client.call_tool(tool_name, {"repo": repo, "max_results": max_items})
                    self.last_poll[poll_key] = now
                    results.append(
                        {
                            "source": f"github:{repo}",
                            "type": "mcp",
                            "data": raw_mcp_result,
                            "extractor": "github_activity",
                            "metadata": {
                                "repo": repo,
                                "event_type": event_type,
                                "repo_config": repo_config,
                            },
                        }
                    )
                except Exception as e:
                    logger.error(f"Watch repo '{repo}' ({event_type}) poll failed: {e}", exc_info=True)

        return results

    async def _poll_source(self, source: dict) -> dict:
        """Dispatch a single source poll to the right executor."""
        if source["type"] == "mcp":
            if not self.mcp_registry:
                raise ValueError("mcp_registry is not initialized for MCP source")

            server_name = source["server"]
            tool_name = source["tool"]

            if server_name == "jobspy":
                job_profiles = self.config.get("job_search", {}).get("profiles", [])
                if job_profiles:
                    profile = job_profiles[0]  # Multi-profile support is Phase D Step 2
                    args = {
                        "query": f"{profile['query']} {profile.get('location', '')}".strip(),
                        "results_wanted": source.get("args", {}).get("results_wanted", 10),
                    }
                else:
                    args = source.get("args", {})
            else:
                args = source.get("args", {})

            client = self.mcp_registry.get_client(server_name)
            if not client:
                raise ValueError(f"MCP client for '{server_name}' not found")

            return cast(dict[Any, Any], client.call_tool(tool_name, args))
        else:
            # Native tool — dispatch through executor
            tool_name = source["tool"]
            args = source.get("args", {})
            return cast(dict[Any, Any], self.executor.execute(tool_name, args))
