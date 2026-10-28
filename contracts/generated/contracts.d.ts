// Generated from contracts/*.schema.json by tools/contracts.py. Do not edit.
// One schema, three languages: this file is what the interface may rely on.

// contracts/interfaces.schema.json

/** Plugin manifest */
export interface PluginManifest {
  "name": string;
  "version": string;
  "description"?: string;
  "author"?: string;
  "entry": string;
  "kind"?: "python" | "declarative";
  "capabilities": Array<string>;
  "actions"?: Array<string>;
  "depends_on"?: Array<string>;
  "created_at"?: number;
  "schema_version": number;
  "source"?: "forged" | "manual" | "imported";
  "task"?: string;
  "runtime"?: string;
  "contracts"?: Record<string, unknown>;
}

/** Native helper RPC request */
export interface NativeRpcRequest {
  "id": string;
  "method": "input.key" | "input.click" | "input.move" | "input.scroll" | "window.list" | "window.focus" | "window.state" | "window.injected_flag" | "screen.capture" | "hotkey.register" | "process.start" | "process.stop" | "process.list";
  "params": Record<string, unknown>;
  "deadline_ms"?: number;
  "requires_uninjected"?: boolean;
}

/** Native helper RPC response */
export interface NativeRpcResponse {
  "id": string;
  "ok": boolean;
  "result"?: string;
  "error"?: string;
  "channel"?: "os_query" | "screen_diff" | "injected_flag" | "none";
  "observed"?: Record<string, unknown>;
  "duration_ms"?: number;
}
// contracts/records.schema.json

/** Evidence receipt (spec 4.2) */
export interface Receipt {
  "receipt_id": string;
  "tool": string;
  "agent_id"?: string;
  "capability"?: string;
  "group"?: string;
  "risk_tier"?: "harmless" | "low" | "moderate" | "irreversible" | "";
  "args"?: Record<string, unknown>;
  "target"?: string;
  "rung": number;
  "rung_name"?: "api" | "accessibility" | "structured-ui" | "vision" | "coordinates";
  "method"?: string;
  "channels"?: Array<string>;
  "started_at": number;
  "ended_at"?: number;
  "duration"?: number;
  "state_before"?: Record<string, unknown>;
  "state_after"?: Record<string, unknown>;
  "outcome": "VERIFIED" | "UNVERIFIED" | "FAILED";
  "verified"?: boolean;
  "error"?: string;
  "output_digest"?: string;
  "task_id"?: string;
  "idempotency_key"?: string;
  "notes"?: string;
}

/** Action journal entry (spec 4.5) */
export interface JournalEntry {
  "entry_id": number;
  "task_id"?: string;
  "agent_id"?: string;
  "action": string;
  "capability"?: string;
  "target"?: string;
  "risk"?: number;
  "mode"?: "background" | "isolated" | "direct" | "";
  "reversible"?: boolean;
  "idempotency_key"?: string;
  "state": "intended" | "running" | "committed" | "failed" | "abandoned" | "rejected";
  "outcome"?: string;
  "error"?: string;
  "notes"?: string;
  "started_at": number;
  "ended_at"?: number | null;
  "evidence"?: Record<string, unknown>;
}

/** Success criterion (spec 4.8) */
export interface Criterion {
  "kind": string;
  "target"?: string;
  "expected"?: string;
  "note"?: string;
  "weight"?: number;
}

/** Task contract (spec 4.8) */
export interface TaskContract {
  "task_id": string;
  "request": string;
  "criteria": Array<Criterion>;
  "intended_capabilities"?: Array<string>;
  "requested_by"?: string;
  "created_at": number;
}

/** Bus event (spec 3.6) */
export interface Event {
  "topic": string;
  "payload"?: Record<string, unknown>;
  "source"?: string;
  "severity": "debug" | "info" | "notice" | "warning" | "error" | "critical";
  "ts": number;
  "ts_iso"?: string;
  "id": string;
}

/** Tool catalog entry (spec 3.4) */
export interface Tool {
  "name": string;
  "description": string;
  "capability": string;
  "parameters": Record<string, unknown>;
  "requires_confirm"?: boolean;
  "danger": "low" | "medium" | "high";
  "group": string;
  "risk_tier"?: "harmless" | "low" | "moderate" | "irreversible";
}

/** HUD scene request (spec 3.6, 17) */
export interface Scene {
  "kind": string;
  "title"?: string;
  "payload"?: Record<string, unknown>;
}

/** Memory record (spec 3.4, memory format) */
export interface MemoryRecord {
  "id"?: number;
  "agent_id": string;
  "kind": string;
  "text": string;
  "tags"?: Array<string>;
  "importance"?: number;
  "ts": number;
  "age_s"?: number;
  "score"?: number;
}
