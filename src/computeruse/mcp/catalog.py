"""Curated MCP catalog and configuration management (pure data and strict types)."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Final, cast

DEFAULT_CONFIG_PATH: Final[Path] = Path.home() / ".computeruse" / "mcp.json"


@dataclass(frozen=True)
class McpFormField:
    """Specification for an environment variable or argument field."""

    key: str
    label: str
    description: str
    secret: bool = False
    default: str = ""


@dataclass(frozen=True)
class McpCatalogItem:
    """A curated, verified MCP server specification."""

    id: str
    name: str
    category: str
    description: str
    icon: str
    command: str
    args: tuple[str, ...] = ()
    env_fields: tuple[McpFormField, ...] = ()
    arg_fields: tuple[McpFormField, ...] = ()


CURATED_CATALOG: Final[tuple[McpCatalogItem, ...]] = (
    # --- Featured ---
    McpCatalogItem(
        id="filesystem",
        name="Local Filesystem",
        category="Featured",
        description="Read, write, and manage files and directories on your Mac.",
        icon="folder",
        command="npx",
        args=("-y", "@modelcontextprotocol/server-filesystem", "{path}"),
        arg_fields=(
            McpFormField(
                key="path",
                label="Allowed Directory Path",
                description="Root directory the agent can access (e.g. /Users/username/Desktop).",
                # Resolved from whoever is running this, not baked in: a
                # personal path shipped as a default is both a leak in a
                # public repository and wrong on every other machine.
                default=str(Path.home() / "Desktop"),
            ),
        ),
    ),
    McpCatalogItem(
        id="memory",
        name="Knowledge Graph Memory",
        category="Featured",
        description="Persistent long-term knowledge graph memory across sessions.",
        icon="brain",
        command="npx",
        args=("-y", "@modelcontextprotocol/server-memory"),
    ),
    McpCatalogItem(
        id="brave-search",
        name="Brave Search",
        category="Featured",
        description="Search the web using Brave Search API with ranked results.",
        icon="globe",
        command="npx",
        args=("-y", "@modelcontextprotocol/server-brave-search"),
        env_fields=(
            McpFormField(
                key="BRAVE_API_KEY",
                label="Brave Search API Key",
                description="Your Brave Search API key from api.search.brave.com.",
                secret=True,
            ),
        ),
    ),
    McpCatalogItem(
        id="tavily",
        name="Tavily AI Search",
        category="Featured",
        description="Fast, comprehensive, real-time AI-optimized web search engine.",
        icon="globe",
        command="npx",
        args=("-y", "tavily-mcp@latest"),
        env_fields=(
            McpFormField(
                key="TAVILY_API_KEY",
                label="Tavily API Key",
                description="Your Tavily API key from tavily.com (tvly-...).",
                secret=True,
            ),
        ),
    ),
    McpCatalogItem(
        id="exa",
        name="Exa Neural Search",
        category="Featured",
        description="Semantic web search, content scraping, and neural research for AI.",
        icon="globe",
        command="npx",
        args=("-y", "exa-mcp-server"),
        env_fields=(
            McpFormField(
                key="EXA_API_KEY",
                label="Exa API Key",
                description="Your Exa API key from dashboard.exa.ai.",
                secret=True,
            ),
        ),
    ),
    McpCatalogItem(
        id="github",
        name="GitHub",
        category="Featured",
        description="Interact with GitHub repositories, issues, PRs, and contents.",
        icon="github",
        command="npx",
        args=("-y", "@modelcontextprotocol/server-github"),
        env_fields=(
            McpFormField(
                key="GITHUB_PERSONAL_ACCESS_TOKEN",
                label="Personal Access Token (classic or fine-grained)",
                description="GitHub token with repo and user permissions.",
                secret=True,
            ),
        ),
    ),
    # --- Productivity ---
    McpCatalogItem(
        id="fetch",
        name="Web Fetch",
        category="Productivity",
        description="Directly fetch and convert web pages to readable markdown.",
        icon="download",
        command="npx",
        args=("-y", "@modelcontextprotocol/server-fetch"),
    ),
    McpCatalogItem(
        id="notion",
        name="Notion",
        category="Productivity",
        description="Read, search, and edit pages and databases in Notion.",
        icon="notion",
        command="npx",
        args=("-y", "@modelcontextprotocol/server-notion"),
        env_fields=(
            McpFormField(
                key="NOTION_API_TOKEN",
                label="Notion Integration Token",
                description="Internal integration token from notion.so/my-integrations.",
                secret=True,
            ),
        ),
    ),
    McpCatalogItem(
        id="slack",
        name="Slack",
        category="Productivity",
        description="Send and read messages across channels in Slack workspaces.",
        icon="slack",
        command="npx",
        args=("-y", "@modelcontextprotocol/server-slack"),
        env_fields=(
            McpFormField(
                key="SLACK_BOT_TOKEN",
                label="Slack Bot User OAuth Token (xoxb-...)",
                description="Bot token with channels:history and chat:write permissions.",
                secret=True,
            ),
            McpFormField(
                key="SLACK_TEAM_ID",
                label="Slack Workspace Team ID (T...)",
                description="Workspace ID from Slack web URL.",
                secret=False,
            ),
        ),
    ),
    McpCatalogItem(
        id="linear",
        name="Linear",
        category="Productivity",
        description="Manage issues, projects, and cycles in Linear.",
        icon="linear",
        command="npx",
        args=("-y", "linear-mcp-server"),
        env_fields=(
            McpFormField(
                key="LINEAR_API_KEY",
                label="Linear API Key",
                description="Personal API key from Linear Settings > API.",
                secret=True,
            ),
        ),
    ),
    # --- Developer Tools ---
    McpCatalogItem(
        id="sqlite",
        name="SQLite Database",
        category="Developer",
        description="Inspect, query, and modify SQLite database files.",
        icon="database",
        command="npx",
        args=("-y", "@modelcontextprotocol/server-sqlite", "{db_path}"),
        arg_fields=(
            McpFormField(
                key="db_path",
                label="Database File Path",
                description="Absolute path to the .sqlite or .db file.",
                default="",
            ),
        ),
    ),
    McpCatalogItem(
        id="postgres",
        name="PostgreSQL",
        category="Developer",
        description="Query tables, schema, and rows from a PostgreSQL database.",
        icon="database",
        command="npx",
        args=("-y", "@modelcontextprotocol/server-postgres", "{connection_string}"),
        arg_fields=(
            McpFormField(
                key="connection_string",
                label="Postgres Connection URI",
                description="postgresql://user:password@localhost:5432/dbname",
                secret=True,
            ),
        ),
    ),
    McpCatalogItem(
        id="git",
        name="Git Tools",
        category="Developer",
        description="Inspect Git repository status, history, diffs, and branches.",
        icon="git",
        command="npx",
        args=("-y", "mcp-server-git", "{repo_path}"),
        arg_fields=(
            McpFormField(
                key="repo_path",
                label="Local Git Repository Path",
                description="Path to a cloned git repository.",
                default="",
            ),
        ),
    ),
    McpCatalogItem(
        id="puppeteer",
        name="Puppeteer Browser",
        category="Developer",
        description="Headless browser automation, navigation, and console scraping.",
        icon="browser",
        command="npx",
        args=("-y", "@modelcontextprotocol/server-puppeteer"),
    ),
    McpCatalogItem(
        id="playwright",
        name="Playwright Browser",
        category="Developer",
        description="Modern headless browser with accessibility snapshots; better for dynamic pages than Puppeteer.",
        icon="browser",
        command="npx",
        args=("-y", "@playwright/mcp@latest"),
    ),
    McpCatalogItem(
        id="sequential-thinking",
        name="Sequential Thinking",
        category="Reasoning",
        description="Step-by-step reasoning scratchpad that improves multi-step planning before acting.",
        icon="brain",
        command="npx",
        args=("-y", "@modelcontextprotocol/server-sequential-thinking"),
    ),
    McpCatalogItem(
        id="time",
        name="Time & Timezone",
        category="Productivity",
        description="Current time, timezone conversion and scheduling helpers for time-aware tasks.",
        icon="clock",
        command="npx",
        args=("-y", "@modelcontextprotocol/server-time"),
    ),
    McpCatalogItem(
        id="google-maps",
        name="Google Maps",
        category="Productivity",
        description="Geocode addresses, search places and get directions.",
        icon="globe",
        command="npx",
        args=("-y", "@modelcontextprotocol/server-google-maps"),
        env_fields=(
            McpFormField(
                key="GOOGLE_MAPS_API_KEY",
                label="Google Maps API Key",
                description="API key from Google Cloud Console with Maps + Places enabled.",
                secret=True,
            ),
        ),
    ),
    McpCatalogItem(
        id="google-drive",
        name="Google Drive",
        category="Productivity",
        description="List, search, read and create Google Drive files and Docs.",
        icon="drive",
        command="npx",
        args=("-y", "@modelcontextprotocol/server-gdrive"),
        env_fields=(
            McpFormField(
                key="GDRIVE_CREDENTIALS_PATH",
                label="Credentials JSON Path",
                description="OAuth client credentials file from Google Cloud Console.",
                secret=False,
            ),
        ),
    ),
    McpCatalogItem(
        id="gmail",
        name="Gmail",
        category="Productivity",
        description="Search, read, draft and send Gmail messages (auto-auth, local credentials).",
        icon="mail",
        command="npx",
        args=("-y", "@gongrzhe/server-gmail-autoauth-mcp"),
    ),
    McpCatalogItem(
        id="youtube-transcript",
        name="YouTube Transcript",
        category="Media",
        description="Fetch transcripts/subtitles from YouTube videos for summarising without watching.",
        icon="video",
        command="npx",
        args=("-y", "@kimtaeyoon83/mcp-server-youtube-transcript"),
    ),
    McpCatalogItem(
        id="obsidian",
        name="Obsidian Vault",
        category="Productivity",
        description="Read, search and append Markdown notes in a local Obsidian vault.",
        icon="notion",
        command="npx",
        args=("-y", "mcp-obsidian", "{vault_path}"),
        arg_fields=(
            McpFormField(
                key="vault_path",
                label="Obsidian Vault Path",
                description="Absolute path to the vault directory.",
                default="",
            ),
        ),
    ),
)


def read_mcp_config(config_path: Path = DEFAULT_CONFIG_PATH) -> dict[str, dict[str, object]]:
    """Read the configured MCP servers from disk (pure)."""
    if not config_path.is_file():
        return {}
    try:
        content = config_path.read_text(encoding="utf-8")
        parsed: object = json.loads(content)
        if isinstance(parsed, dict):
            parsed_dict = cast("dict[str, object]", parsed)
            servers_raw = parsed_dict.get("mcpServers")
            if isinstance(servers_raw, dict):
                servers_dict = cast("dict[object, object]", servers_raw)
                result: dict[str, dict[str, object]] = {}
                for k, v in servers_dict.items():
                    if isinstance(k, str) and isinstance(v, dict):
                        result[k] = cast("dict[str, object]", v)
                return result
        return {}
    except (json.JSONDecodeError, OSError):
        return {}


def write_mcp_server(
    server_id: str,
    command: str,
    args: list[str],
    env: dict[str, str],
    config_path: Path = DEFAULT_CONFIG_PATH,
) -> None:
    """Add or update an MCP server in ~/.computeruse/mcp.json."""
    config_path.parent.mkdir(parents=True, exist_ok=True)
    existing: dict[str, object] = {}
    if config_path.is_file():
        try:
            raw: object = json.loads(config_path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                existing = cast("dict[str, object]", raw)
        except (json.JSONDecodeError, OSError):
            existing = {}

    servers_obj = existing.get("mcpServers")
    servers: dict[str, object] = (
        cast("dict[str, object]", servers_obj) if isinstance(servers_obj, dict) else {}
    )

    server_entry: dict[str, object] = {
        "command": command,
        "args": args,
    }
    if env:
        server_entry["env"] = env

    servers[server_id] = server_entry
    existing["mcpServers"] = servers

    config_path.write_text(json.dumps(existing, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def remove_mcp_server(server_id: str, config_path: Path = DEFAULT_CONFIG_PATH) -> bool:
    """Remove an MCP server from ~/.computeruse/mcp.json."""
    if not config_path.is_file():
        return False
    try:
        raw: object = json.loads(config_path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            return False
        raw_dict = cast("dict[str, object]", raw)
        servers_obj = raw_dict.get("mcpServers")
        if not isinstance(servers_obj, dict):
            return False
        servers = cast("dict[str, object]", servers_obj)
        if server_id in servers:
            del servers[server_id]
            raw_dict["mcpServers"] = servers
            config_path.write_text(json.dumps(raw_dict, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
            return True
        return False
    except (json.JSONDecodeError, OSError):
        return False


def serialize_catalog() -> list[dict[str, object]]:
    """Serialize the curated catalog for webview transport."""
    result: list[dict[str, object]] = []
    for item in CURATED_CATALOG:
        env_list: list[dict[str, object]] = [
            {
                "key": f.key,
                "label": f.label,
                "description": f.description,
                "secret": f.secret,
                "default": f.default,
            }
            for f in item.env_fields
        ]
        arg_list: list[dict[str, object]] = [
            {
                "key": f.key,
                "label": f.label,
                "description": f.description,
                "secret": f.secret,
                "default": f.default,
            }
            for f in item.arg_fields
        ]
        result.append(
            {
                "id": item.id,
                "name": item.name,
                "category": item.category,
                "description": item.description,
                "icon": item.icon,
                "command": item.command,
                "args": list(item.args),
                "env_fields": env_list,
                "arg_fields": arg_list,
            }
        )
    return result
