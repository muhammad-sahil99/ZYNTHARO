"""Token-based connectors: the platforms a business already pays for.

Each of these works the same way, and it is the honest way for a desktop product: the customer
creates one token inside their own account — a "private app", a "personal access token", an
"internal integration" — and pastes it once. No app review, no client secret in the binary, no
OAuth redirect server, and it works the same for a one-person office and a fifty-seat company.

That is also why these are real rather than aspirational: every operation below is an endpoint the
provider documents for token holders, and the transport is the shared, tested HTTP client. What
this module deliberately does **not** do is invent endpoints. If an operation is not in the table,
calling it is an error naming the ones that exist.

Platforms that *cannot* work this way — Instagram, Messenger, X, LinkedIn, TikTok, Snapchat — are
not here at all; :mod:`orbynth.channels.registry` declares their business gates so the product
never pretends otherwise.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from .base import AUTH_KEY_TOKEN, AUTH_TOKEN, Connector, ConnectorError, Operation, READY
from .transport import fill_path, request_json

READ = "connector.read"
WRITE = "connector.write"
SEND = "connector.send"


@dataclass(slots=True)
class OpSpec:
    name: str
    description: str
    method: str
    path: str
    capability: str = READ
    #: Arguments that fill ``{placeholders}`` in the path.
    path_args: tuple[str, ...] = ()
    #: Arguments placed in the query string (GET) or the JSON body (POST/PATCH/PUT).
    fields: tuple[str, ...] = ()
    #: Provider-specific headers that must accompany this call.
    headers: dict[str, str] = field(default_factory=dict)

    def to_operation(self, connector: str) -> Operation:
        return Operation(
            name=self.name,
            description=self.description,
            capability=self.capability,
            method=self.method,
            path=f"{connector}:{self.path}",
            mutates=self.method.upper() not in {"GET", "HEAD"},
        )


@dataclass(slots=True)
class SaasSpec:
    name: str
    title: str
    category: str
    base_url: str
    needs: str
    where: str
    docs: str
    verify: str
    operations: tuple[OpSpec, ...]
    #: ``bearer`` — Authorization: Bearer <token>; ``raw_header`` — a custom header; ``query`` —
    #: key+token in the query string (Trello); ``basic`` — basic auth with token as password.
    style: str = "bearer"
    header_name: str = "Authorization"
    header_prefix: str = "Bearer "
    key_name: str = "key"
    auth: str = AUTH_TOKEN
    extra_headers: dict[str, str] = field(default_factory=dict)
    #: For key+token services such as Trello the token is the second value.
    token_field: str = "token"

    def operations_map(self) -> dict[str, OpSpec]:
        return {op.name: op for op in self.operations}


def _op(name: str, description: str, method: str, path: str, capability: str = READ, path_args: tuple[str, ...] = (), fields: tuple[str, ...] = (), headers: dict[str, str] | None = None) -> OpSpec:
    return OpSpec(name, description, method, path, capability, path_args, fields, headers or {})


SLACK = SaasSpec(
    name="slack",
    title="Slack",
    category="chat",
    base_url="https://slack.com/api",
    needs="A Slack app in your workspace with a bot token (xoxb-…) and the scopes you want to grant.",
    where="api.slack.com/apps → create app → OAuth & Permissions → install → copy the Bot User OAuth Token.",
    docs="https://api.slack.com/authentication/token-types",
    verify="auth_test",
    style="bearer",
    operations=(
        _op("auth_test", "Which workspace and bot this token belongs to.", "POST", "/auth.test"),
        _op("list_channels", "Public channels the bot can see.", "GET", "/conversations.list", fields=("types", "limit")),
        _op("history", "Recent messages in a channel.", "GET", "/conversations.history", path_args=(), fields=("channel", "limit")),
        _op("post_message", "Post a message to a channel. Cannot be recalled.", "POST", "/chat.postMessage", SEND, fields=("channel", "text", "thread_ts")),
        _op("list_users", "Workspace members.", "GET", "/users.list", fields=("limit",)),
        _op("open_dm", "Open a direct message channel with a user.", "POST", "/conversations.open", WRITE, fields=("users",)),
        _op("search_messages", "Search messages (needs the search:read user scope).", "GET", "/search.messages", fields=("query", "count")),
    ),
)

NOTION = SaasSpec(
    name="notion",
    title="Notion",
    category="work",
    base_url="https://api.notion.com/v1",
    needs="An internal integration token, and each page/database shared with that integration.",
    where="notion.so/profile/integrations → new integration → copy the Internal Integration Secret.",
    docs="https://developers.notion.com/reference/authentication",
    verify="search",
    extra_headers={"Notion-Version": "2022-06-28"},
    operations=(
        _op("search", "Find pages and databases shared with the integration.", "POST", "/search", fields=("query", "page_size")),
        _op("get_page", "One page's properties.", "GET", "/pages/{page_id}", path_args=("page_id",)),
        _op("query_database", "Rows of a database, with optional filter/sorts.", "POST", "/databases/{database_id}/query", path_args=("database_id",), fields=("filter", "sorts", "page_size")),
        _op("create_page", "Create a page in a database or under a page.", "POST", "/pages", WRITE, fields=("parent", "properties", "children")),
        _op("append_blocks", "Append content blocks to a page.", "PATCH", "/blocks/{block_id}/children", WRITE, path_args=("block_id",), fields=("children",)),
        _op("update_page", "Change a page's properties (status, owner, dates).", "PATCH", "/pages/{page_id}", WRITE, path_args=("page_id",), fields=("properties", "archived")),
        _op("list_users", "People in the workspace.", "GET", "/users", fields=("page_size",)),
    ),
)

AIRTABLE = SaasSpec(
    name="airtable",
    title="Airtable",
    category="work",
    base_url="https://api.airtable.com/v0",
    needs="A personal access token with the bases it may touch, plus the base id.",
    where="airtable.com/create/tokens → create token → add scopes (data.records:read/write) and bases.",
    docs="https://airtable.com/developers/web/api/authentication",
    verify="list_records",
    operations=(
        _op("list_records", "Records from a table.", "GET", "/{base_id}/{table}", fields=("maxRecords", "view", "filterByFormula", "pageSize")),
        _op("get_record", "One record.", "GET", "/{base_id}/{table}/{record_id}", path_args=("record_id",)),
        _op("create_records", "Create up to ten records.", "POST", "/{base_id}/{table}", WRITE, fields=("records", "typecast")),
        _op("update_records", "Change fields on up to ten records.", "PATCH", "/{base_id}/{table}", WRITE, fields=("records", "typecast")),
        _op("delete_records", "Delete records by id. Not undoable from ORBYNTH.", "DELETE", "/{base_id}/{table}", "filesystem.delete", fields=("records",)),
        _op("list_bases", "Bases this token can see.", "GET", "/meta/bases", fields=()),
    ),
)

TRELLO = SaasSpec(
    name="trello",
    title="Trello",
    category="work",
    base_url="https://api.trello.com/1",
    needs="An API key and a token from the same Trello account.",
    where="trello.com/power-ups/admin → create a Power-Up to get a key, then generate a token.",
    docs="https://developer.atlassian.com/cloud/trello/guides/rest-api/authorization/",
    verify="member",
    style="query",
    auth=AUTH_KEY_TOKEN,
    key_name="key",
    token_field="token",
    operations=(
        _op("member", "The account this key/token belongs to.", "GET", "/members/me"),
        _op("boards", "Boards this account can open.", "GET", "/members/me/boards", fields=("filter",)),
        _op("lists", "Lists (columns) on a board.", "GET", "/boards/{board_id}/lists", path_args=("board_id",)),
        _op("cards", "Cards in a list or on a board.", "GET", "/lists/{list_id}/cards", path_args=("list_id",), fields=("fields",)),
        _op("create_card", "Add a card to a list.", "POST", "/cards", WRITE, fields=("idList", "name", "desc", "due", "idMembers")),
        _op("comment", "Comment on a card.", "POST", "/cards/{card_id}/actions/comments", WRITE, path_args=("card_id",), fields=("text",)),
        _op("move_card", "Move a card to another list.", "PUT", "/cards/{card_id}", WRITE, path_args=("card_id",), fields=("idList", "pos")),
        _op("archive_card", "Archive a card.", "PUT", "/cards/{card_id}", WRITE, path_args=("card_id",), fields=("closed",)),
    ),
)

ASANA = SaasSpec(
    name="asana",
    title="Asana",
    category="work",
    base_url="https://app.asana.com/api/1.0",
    needs="A personal access token from your Asana developer console.",
    where="app.asana.com/0/my-apps → create a personal access token.",
    docs="https://developers.asana.com/docs/personal-access-token",
    verify="me",
    operations=(
        _op("me", "The account this token belongs to.", "GET", "/users/me"),
        _op("workspaces", "Workspaces you belong to.", "GET", "/workspaces", fields=("limit",)),
        _op("projects", "Projects in a workspace.", "GET", "/workspaces/{workspace_id}/projects", path_args=("workspace_id",), fields=("limit",)),
        _op("tasks", "Tasks in a project, with assignee and due date.", "GET", "/projects/{project_id}/tasks", path_args=("project_id",), fields=("limit", "opt_fields")),
        _op("create_task", "Create a task.", "POST", "/tasks", WRITE, fields=("data")),
        _op("update_task", "Update a task (assignee, due date, completion).", "PUT", "/tasks/{task_id}", WRITE, path_args=("task_id",), fields=("data",)),
        _op("comment", "Comment on a task.", "POST", "/tasks/{task_id}/stories", WRITE, path_args=("task_id",), fields=("data",)),
        _op("search_tasks", "Search tasks in a workspace.", "GET", "/workspaces/{workspace_id}/tasks/search", path_args=("workspace_id",), fields=("text", "limit")),
    ),
)

JIRA = SaasSpec(
    name="jira",
    title="Jira Cloud",
    category="work",
    base_url="https://your-domain.atlassian.net/rest/api/3",
    needs="Your site's base URL, your account email, and an API token.",
    where="id.atlassian.com/manage-profile/security/api-tokens → create token. Basic auth: email + token.",
    docs="https://developer.atlassian.com/cloud/jira/platform/basic-auth-for-rest-apis/",
    verify="myself",
    style="basic",
    auth=AUTH_KEY_TOKEN,
    token_field="api_token",
    operations=(
        _op("myself", "The account this email/token belongs to.", "GET", "/myself"),
        _op("search", "JQL search across issues.", "GET", "/search", fields=("jql", "maxResults", "fields")),
        _op("issue", "One issue, with description and status.", "GET", "/issue/{key}", path_args=("key",), fields=("fields",)),
        _op("create_issue", "Create an issue.", "POST", "/issue", WRITE, fields=("fields",)),
        _op("comment", "Comment on an issue.", "POST", "/issue/{key}/comment", WRITE, path_args=("key",), fields=("body",)),
        _op("transitions", "Available status transitions for an issue.", "GET", "/issue/{key}/transitions", path_args=("key",)),
        _op("transition_issue", "Move an issue to another status.", "POST", "/issue/{key}/transitions", WRITE, path_args=("key",), fields=("transition",)),
        _op("assign", "Assign an issue to an account id.", "PUT", "/issue/{key}/assignee", WRITE, path_args=("key",), fields=("accountId",)),
    ),
)

GITHUB = SaasSpec(
    name="github",
    title="GitHub",
    category="code",
    base_url="https://api.github.com",
    needs="A fine-grained personal access token with only the repositories it needs.",
    where="github.com/settings/tokens → generate a fine-grained token with the repo scopes you want.",
    docs="https://docs.github.com/en/rest/authentication/authenticating-to-the-rest-api",
    verify="user",
    extra_headers={"X-GitHub-Api-Version": "2022-11-28"},
    operations=(
        _op("user", "The authenticated account.", "GET", "/user"),
        _op("repos", "Repositories this token can see.", "GET", "/user/repos", fields=("per_page", "sort", "affiliation")),
        _op("issues", "Issues in a repository.", "GET", "/repos/{owner}/{repo}/issues", path_args=("owner", "repo"), fields=("state", "per_page", "labels")),
        _op("create_issue", "Open an issue.", "POST", "/repos/{owner}/{repo}/issues", WRITE, path_args=("owner", "repo"), fields=("title", "body", "labels", "assignees")),
        _op("comment", "Comment on an issue or pull request.", "POST", "/repos/{owner}/{repo}/issues/{number}/comments", WRITE, path_args=("owner", "repo", "number"), fields=("body",)),
        _op("pulls", "Pull requests in a repository.", "GET", "/repos/{owner}/{repo}/pulls", path_args=("owner", "repo"), fields=("state", "per_page")),
        _op("workflow_runs", "Recent CI runs.", "GET", "/repos/{owner}/{repo}/actions/runs", path_args=("owner", "repo"), fields=("per_page", "status")),
        _op("notifications", "Your unread notifications.", "GET", "/notifications", fields=("all", "per_page")),
    ),
)

LINEAR = SaasSpec(
    name="linear",
    title="Linear",
    category="work",
    base_url="https://api.linear.app",
    needs="A personal API key from Linear settings.",
    where="linear.app/settings/api → create a personal API key.",
    docs="https://linear.app/developers/graphql",
    verify="viewer",
    operations=(
        _op("viewer", "The account this key belongs to (GraphQL).", "POST", "/graphql", fields=("query", "variables")),
        _op("graphql", "Any read-only GraphQL query for this workspace.", "POST", "/graphql", fields=("query", "variables")),
        _op("create_issue", "Create an issue through GraphQL.", "POST", "/graphql", WRITE, fields=("query", "variables")),
    ),
)

HUBSPOT = SaasSpec(
    name="hubspot",
    title="HubSpot",
    category="crm",
    base_url="https://api.hubapi.com",
    needs="A private app access token from HubSpot settings.",
    where="HubSpot → Settings → Integrations → Private Apps → create app → copy the access token.",
    docs="https://developers.hubspot.com/docs/api/private-apps",
    verify="contacts",
    operations=(
        _op("contacts", "Recent contacts, with the properties you name.", "GET", "/crm/v3/objects/contacts", fields=("limit", "properties", "after")),
        _op("deals", "Recent deals.", "GET", "/crm/v3/objects/deals", fields=("limit", "properties", "after")),
        _op("companies", "Recent companies.", "GET", "/crm/v3/objects/companies", fields=("limit", "properties")),
        _op("create_contact", "Create a contact.", "POST", "/crm/v3/objects/contacts", WRITE, fields=("properties",)),
        _op("update_contact", "Update a contact's properties.", "PATCH", "/crm/v3/objects/contacts/{contact_id}", WRITE, path_args=("contact_id",), fields=("properties",)),
        _op("search", "Search any CRM object.", "POST", "/crm/v3/objects/{object_type}/search", path_args=("object_type",), fields=("filterGroups", "sorts", "limit", "properties")),
        _op("engagements", "Log a note against a record.", "POST", "/crm/v3/objects/notes", WRITE, fields=("properties", "associations")),
    ),
)

STRIPE = SaasSpec(
    name="stripe",
    title="Stripe",
    category="payments",
    base_url="https://api.stripe.com/v1",
    needs="A restricted API key. Never paste a full secret key into a desktop app if a restricted key will do.",
    where="dashboard.stripe.com/apikeys → create a restricted key with read scopes (plus write only if needed).",
    docs="https://docs.stripe.com/keys",
    verify="balance",
    operations=(
        _op("balance", "Current account balance.", "GET", "/balance"),
        _op("customers", "Recent customers.", "GET", "/customers", fields=("limit", "email")),
        _op("invoices", "Recent invoices, with status and amount.", "GET", "/invoices", fields=("limit", "status", "customer")),
        _op("charges", "Recent charges.", "GET", "/charges", fields=("limit", "customer")),
        _op("payment_links", "Payment links you have created.", "GET", "/payment_links", fields=("limit",)),
        _op("create_payment_link", "Create a payment link for a price.", "POST", "/payment_links", SEND, fields=("line_items",)),
        _op("refund", "Refund a charge. Money leaves the account; treat as irreversible.", "POST", "/refunds", "filesystem.delete", fields=("charge", "amount")),
    ),
)

DROPBOX = SaasSpec(
    name="dropbox",
    title="Dropbox",
    category="files",
    base_url="https://api.dropboxapi.com/2",
    needs="A Dropbox app access token (scoped to the files it needs).",
    where="dropbox.com/developers/apps → create app → Permissions → generate an access token.",
    docs="https://www.dropbox.com/developers/documentation/http/documentation",
    verify="account",
    operations=(
        _op("account", "Which Dropbox account this token belongs to.", "POST", "/users/get_current_account"),
        _op("list_folder", "Entries in a folder.", "POST", "/files/list_folder", fields=("path", "limit", "recursive")),
        _op("search", "Search a Dropbox account by name or content.", "POST", "/files/search_v2", fields=("query", "options")),
        _op("create_folder", "Create a folder.", "POST", "/files/create_folder_v2", WRITE, fields=("path",)),
        _op("upload", "Upload a file's text content (small files).", "POST", "/files/upload", WRITE, fields=("content",)),
        _op("shared_links", "Existing shared links.", "POST", "/sharing/list_shared_links", fields=("path",)),
    ),
)

ZOOM = SaasSpec(
    name="zoom",
    title="Zoom",
    category="calendar",
    base_url="https://api.zoom.us/v2",
    needs="A Server-to-Server OAuth app's account id, client id and client secret, or a user OAuth token.",
    where="marketplace.zoom.us → Develop → Build App → Server-to-Server OAuth.",
    docs="https://developers.zoom.us/docs/integrations/oauth/",
    verify="me",
    operations=(
        _op("me", "The user this token acts as.", "GET", "/users/me"),
        _op("meetings", "Upcoming meetings for a user.", "GET", "/users/{user_id}/meetings", path_args=("user_id",), fields=("type", "page_size")),
        _op("create_meeting", "Schedule a meeting.", "POST", "/users/{user_id}/meetings", WRITE, path_args=("user_id",), fields=("topic", "type", "start_time", "duration", "timezone", "agenda")),
        _op("recordings", "Cloud recordings.", "GET", "/users/{user_id}/recordings", path_args=("user_id",), fields=("from", "to")),
        _op("delete_meeting", "Delete a scheduled meeting.", "DELETE", "/meetings/{meeting_id}", "filesystem.delete", path_args=("meeting_id",)),
    ),
)

SALESFORCE = SaasSpec(
    name="salesforce",
    title="Salesforce",
    category="crm",
    base_url="https://your-instance.my.salesforce.com/services/data/v61.0",
    needs="Your instance URL and an access token from a Connected App (OAuth).",
    where="Setup → App Manager → New Connected App → enable OAuth → complete the flow to get a token.",
    docs="https://developer.salesforce.com/docs/atlas.en-us.api_rest.meta/api_rest/",
    verify="limits",
    operations=(
        _op("limits", "API usage against your org's daily limit.", "GET", "/limits"),
        _op("query", "Run a SOQL query.", "GET", "/query", fields=("q",)),
        _op("describe", "Fields of an object.", "GET", "/sobjects/{sobject}/describe", path_args=("sobject",)),
        _op("create", "Create a record.", "POST", "/sobjects/{sobject}", WRITE, path_args=("sobject",), fields=("body",)),
        _op("update", "Update a record.", "PATCH", "/sobjects/{sobject}/{record_id}", WRITE, path_args=("sobject", "record_id"), fields=("body",)),
        _op("recent", "Recently viewed records.", "GET", "/sobjects/{sobject}/recent", path_args=("sobject",)),
    ),
)

DISCORD = SaasSpec(
    name="discord",
    title="Discord",
    category="chat",
    base_url="https://discord.com/api/v10",
    needs="A bot token from the Discord developer portal, with the bot invited to your server.",
    where="discord.com/developers/applications → Bot → Reset Token → copy, then invite the bot.",
    docs="https://discord.com/developers/docs/reference",
    verify="me",
    operations=(
        _op("me", "The bot account.", "GET", "/users/@me"),
        _op("guilds", "Servers the bot is in.", "GET", "/users/@me/guilds"),
        _op("channels", "Channels in a server.", "GET", "/guilds/{guild_id}/channels", path_args=("guild_id",)),
        _op("messages", "Recent messages in a channel.", "GET", "/channels/{channel_id}/messages", path_args=("channel_id",), fields=("limit",)),
        _op("send", "Post a message to a channel.", "POST", "/channels/{channel_id}/messages", SEND, fields=("content",)),
    ),
)

SPECS: tuple[SaasSpec, ...] = (
    SLACK, GITHUB, NOTION, AIRTABLE, TRELLO, ASANA, JIRA, HUBSPOT, STRIPE, DROPBOX, ZOOM, SALESFORCE, LINEAR, DISCORD,
)


class SaasConnector(Connector):
    """One token, one declared surface, one shared transport."""

    def __init__(self, spec: SaasSpec, connection=None):
        super().__init__(connection)
        self.spec = spec
        self.name = spec.name
        self.title = spec.title
        self.category = spec.category
        self.auth = spec.auth
        self.needs = spec.needs
        self.where = spec.where
        self.docs = spec.docs

    # -- credential -------------------------------------------------------------------
    def _headers(self, op: OpSpec, base_url: str) -> tuple[dict[str, str], dict[str, Any]]:
        """Auth headers/params for one call. Basic-style services build an auth header."""
        if self.connection is None:
            raise ConnectorError(f"{self.title} has not been connected yet")
        headers: dict[str, str] = {**self.spec.extra_headers, **op.headers}
        params: dict[str, Any] = {}
        if self.spec.style == "bearer":
            token = self.connection.require("token")
            headers[self.spec.header_name] = f"{self.spec.header_prefix}{token}"
        elif self.spec.style == "query":
            params[self.spec.key_name] = self.connection.require(self.spec.key_name)
            params[self.spec.token_field] = self.connection.require(self.spec.token_field)
        elif self.spec.style == "basic":
            import base64

            user = self.connection.require("email")
            secret = self.connection.require(self.spec.token_field)
            raw = f"{user}:{secret}".encode("utf-8")
            headers["Authorization"] = "Basic " + base64.b64encode(raw).decode("ascii")
        else:  # pragma: no cover - every spec declares a known style
            raise ConnectorError(f"unknown auth style {self.spec.style!r} for {self.name}")
        return headers, params

    def base_url(self) -> str:
        override = (self.connection.values.get("base_url") if self.connection else "") or ""
        return (override or self.spec.base_url).rstrip("/")

    # -- contract ---------------------------------------------------------------------
    def operations(self) -> list[Operation]:
        return [op.to_operation(self.name) for op in self.spec.operations]

    def status(self):
        if self.connection is None:
            return self._needs_setup(f"{self.title} is not connected. {self.needs}")
        for key in self._required_keys():
            if not self.connection.values.get(key):
                return self._needs_setup(f"{self.title} connection is missing '{key}'. {self.needs}")
        account = self.connection.meta.get("account", "")
        if self.connection.verified_at:
            label = f"{self.title}" + (f" as {account}" if account else "")
            return self._ready(f"{label}; credential verified against the service", account)
        return self._configured(f"{self.title} credential stored; run verify to prove it works")

    def _required_keys(self) -> tuple[str, ...]:
        if self.spec.style == "query":
            return (self.spec.key_name, self.spec.token_field)
        if self.spec.style == "basic":
            return ("email", self.spec.token_field)
        return ("token",)

    def verify(self):
        op = self.spec.operations_map().get(self.spec.verify) or next(
            (o for o in self.spec.operations if o.capability == READ), None
        )
        if op is None:  # pragma: no cover - every spec has a read operation
            return self._configured("no verification operation is declared")
        result = self._execute(op, {})
        account = ""
        if isinstance(result, dict):
            for key in ("name", "real_name", "email", "login", "display_name", "username", "url", "id"):
                if isinstance(result.get(key), str) and result.get(key):
                    account = str(result[key])
                    break
            if not account and isinstance(result.get("user"), dict):
                account = str(result["user"].get("name") or result["user"].get("email") or "")
            if not account and isinstance(result.get("team"), dict):
                account = str(result["team"].get("name") or "")
            if not account and isinstance(result.get("results"), list) and result["results"]:
                first = result["results"][0]
                if isinstance(first, dict):
                    account = str(first.get("name") or first.get("email") or "")
        return self._ready(f"{self.title} verified" + (f" as {account}" if account else ""), account)

    def call(self, operation: str, **args: Any) -> Any:
        op = self.spec.operations_map().get(operation)
        if op is None:
            known = ", ".join(sorted(self.spec.operations_map())) or "none"
            raise ConnectorError(f"{self.title} has no operation {operation!r}. Available: {known}")
        return self._execute(op, args)

    # -- execution --------------------------------------------------------------------
    def _execute(self, op: OpSpec, args: dict[str, Any]) -> Any:
        headers, auth_params = self._headers(op, self.base_url())
        url = self.base_url() + fill_path(op.path, args)
        payload: Any = None
        if op.method.upper() in {"GET", "DELETE"}:
            params = {**auth_params, **{k: args[k] for k in op.fields if k in args}}
        else:
            params = dict(auth_params)
            body = {k: args[k] for k in op.fields if k in args}
            payload = body if body else None
            # DELETE with a body is rare; providers that need it accept a query instead.
        if op.fields and op.method.upper() in {"POST", "PATCH", "PUT"} and payload is None:
            raise ConnectorError(
                f"{self.title}.{op.name} needs at least one of: {', '.join(op.fields)}"
            )
        result = request_json(op.method, url, headers=headers, params=params, body=payload)
        if self.spec.name == "slack" and isinstance(result, dict) and result.get("ok") is False:
            raise ConnectorError(f"Slack refused {op.name}: {result.get('error', 'unknown error')}")
        return result


def build_connectors(vault_get: Callable[[str], Any]) -> list[Connector]:
    """Every SaaS connector in this build, wired to the vault's stored credential (if any)."""
    return [SaasConnector(spec, vault_get(spec.name)) for spec in SPECS]
