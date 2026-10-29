// Generated from contracts/*.schema.json by tools/contracts.py. Do not edit.
// The native helper reads and writes exactly these shapes.

using System.Collections.Generic;

namespace Zyntharo.Contracts
{
    // contracts/interfaces.schema.json
    /// <summary>Plugin manifest</summary>
    public sealed record PluginManifest
    {
        public string Name { get; init; }
        public string Version { get; init; }
        public string? Description { get; init; }
        public string? Author { get; init; }
        public string Entry { get; init; }
        public string? Kind { get; init; }
        public List<string> Capabilities { get; init; }
        public List<string>? Actions { get; init; }
        public List<string>? DependsOn { get; init; }
        public double? CreatedAt { get; init; }
        public long SchemaVersion { get; init; }
        public string? Source { get; init; }
        public string? Task { get; init; }
        public string? Runtime { get; init; }
        public Dictionary<string, object?>? Contracts { get; init; }
    }

    /// <summary>Native helper RPC request</summary>
    public sealed record NativeRpcRequest
    {
        public string Id { get; init; }
        public string Method { get; init; }
        public Dictionary<string, object?> Params { get; init; }
        public long? DeadlineMs { get; init; }
        public bool? RequiresUninjected { get; init; }
    }

    /// <summary>Native helper RPC response</summary>
    public sealed record NativeRpcResponse
    {
        public string Id { get; init; }
        public bool Ok { get; init; }
        public string? Result { get; init; }
        public string? Error { get; init; }
        public string? Channel { get; init; }
        public Dictionary<string, object?>? Observed { get; init; }
        public long? DurationMs { get; init; }
    }

    // contracts/records.schema.json
    /// <summary>Evidence receipt (spec 4.2)</summary>
    public sealed record Receipt
    {
        public string ReceiptId { get; init; }
        public string Tool { get; init; }
        public string? AgentId { get; init; }
        public string? Capability { get; init; }
        public string? Group { get; init; }
        public string? RiskTier { get; init; }
        public Dictionary<string, object?>? Args { get; init; }
        public string? Target { get; init; }
        public long Rung { get; init; }
        public string? RungName { get; init; }
        public string? Method { get; init; }
        public List<string>? Channels { get; init; }
        public double StartedAt { get; init; }
        public double? EndedAt { get; init; }
        public double? Duration { get; init; }
        public Dictionary<string, object?>? StateBefore { get; init; }
        public Dictionary<string, object?>? StateAfter { get; init; }
        public string Outcome { get; init; }
        public bool? Verified { get; init; }
        public string? Error { get; init; }
        public string? OutputDigest { get; init; }
        public string? TaskId { get; init; }
        public string? IdempotencyKey { get; init; }
        public string? Notes { get; init; }
    }

    /// <summary>Action journal entry (spec 4.5)</summary>
    public sealed record JournalEntry
    {
        public long EntryId { get; init; }
        public string? TaskId { get; init; }
        public string? AgentId { get; init; }
        public string Action { get; init; }
        public string? Capability { get; init; }
        public string? Target { get; init; }
        public long? Risk { get; init; }
        public string? Mode { get; init; }
        public bool? Reversible { get; init; }
        public string? IdempotencyKey { get; init; }
        public string State { get; init; }
        public string? Outcome { get; init; }
        public string? Error { get; init; }
        public string? Notes { get; init; }
        public double StartedAt { get; init; }
        public double? EndedAt { get; init; }
        public Dictionary<string, object?>? Evidence { get; init; }
    }

    /// <summary>Success criterion (spec 4.8)</summary>
    public sealed record Criterion
    {
        public string Kind { get; init; }
        public string? Target { get; init; }
        public string? Expected { get; init; }
        public string? Note { get; init; }
        public double? Weight { get; init; }
    }

    /// <summary>Task contract (spec 4.8)</summary>
    public sealed record TaskContract
    {
        public string TaskId { get; init; }
        public string Request { get; init; }
        public List<Criterion?> Criteria { get; init; }
        public List<string>? IntendedCapabilities { get; init; }
        public string? RequestedBy { get; init; }
        public double CreatedAt { get; init; }
    }

    /// <summary>Bus event (spec 3.6)</summary>
    public sealed record Event
    {
        public string Topic { get; init; }
        public Dictionary<string, object?>? Payload { get; init; }
        public string? Source { get; init; }
        public string Severity { get; init; }
        public double Ts { get; init; }
        public string? TsIso { get; init; }
        public string Id { get; init; }
    }

    /// <summary>Tool catalog entry (spec 3.4)</summary>
    public sealed record Tool
    {
        public string Name { get; init; }
        public string Description { get; init; }
        public string Capability { get; init; }
        public Dictionary<string, object?> Parameters { get; init; }
        public bool? RequiresConfirm { get; init; }
        public string Danger { get; init; }
        public string Group { get; init; }
        public string? RiskTier { get; init; }
    }

    /// <summary>HUD scene request (spec 3.6, 17)</summary>
    public sealed record Scene
    {
        public string Kind { get; init; }
        public string? Title { get; init; }
        public Dictionary<string, object?>? Payload { get; init; }
    }

    /// <summary>Memory record (spec 3.4, memory format)</summary>
    public sealed record MemoryRecord
    {
        public long? Id { get; init; }
        public string AgentId { get; init; }
        public string Kind { get; init; }
        public string Text { get; init; }
        public List<string>? Tags { get; init; }
        public double? Importance { get; init; }
        public double Ts { get; init; }
        public double? AgeS { get; init; }
        public double? Score { get; init; }
    }

}
