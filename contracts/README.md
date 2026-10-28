# contracts/

Every interface ORBYNTH persists or exposes is defined here first, as JSON Schema, and
versioned in `versions.json`. The code implements these; it does not define them.

| file | holds |
| --- | --- |
| `records.schema.json` | receipt, journal-entry, event, tool, scene, memory-record, task-contract, criterion |
| `interfaces.schema.json` | plugin-manifest, native-rpc-request, native-rpc-response |
| `versions.json` | the version of each contract, its status, and who reads and writes it |
| `generated/` | TypeScript and C# bindings produced from the schemas |

Generated Python bindings live at `orbynth/contracts/generated.py` so they import as part of
the package. Regenerate all three with:

```bash
python3 tools/contracts.py          # write the bindings
python3 tools/contracts.py --check  # fail if the schemas and bindings disagree
```

The release gate check `contracts` runs the `--check` comparison, validates every live tool
catalogue row against `tool`, and confirms that a receipt with no observation channel is
graded `UNVERIFIED`. `tests/test_contracts.py` additionally validates real receipts, journal
entries, events, memory records and task contracts produced by the running system.

## Changing a contract

1. Edit the definition in the schema file.
2. Bump its version in `versions.json`:
   * **major** — an existing reader cannot parse the new shape,
   * **minor** — additive and readable by the old reader,
   * **patch** — documentation or a tighter constraint only.
3. If the shape is stored on disk (`status: frozen`), add a migration with a tested rollback,
   and keep reading the old format for one release.
4. Run `python3 tools/contracts.py` and commit the regenerated bindings with the schema.

A version mismatch at runtime is decided by `orbynth/contracts/compat.py`: the same major is
readable (a newer minor only warns), a different major is refused with the migration named,
and an unparsable or unknown contract name is refused. Plugins declare the versions they were
written against in their manifest, and the loader refuses an incompatible one at install time.

## Notes on honesty

These schemas describe what the code already produces. They are not aspirations: if a field
is required here, a writer that omits it fails the test suite. The validator supports only a
documented subset of JSON Schema, and it raises on any keyword outside that subset rather than
ignoring it, because a silently ignored keyword turns a contract into a comment.