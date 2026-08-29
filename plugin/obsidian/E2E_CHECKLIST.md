# obsidian plugin — E2E checklist (Lift Q3-Obsidian)

Non-web component: the end-to-end surface is the plugin loaded by
`PluginLoader` and dispatched through `PluginRunner` exactly as the runtime
will. Each item is verified by an automated test — no real Obsidian files,
no LLM, no network. Re-run:

```bash
uv run pytest plugin/obsidian/tests -q
```

- [x] Plugin is discoverable by the loader (no central registry edit)
      — `test_plugin.py::TestLoaderDiscovery::test_loader_discovers_obsidian`
- [x] Declares no required credentials
      — `TestPluginMeta`
- [x] `@p.setup` reads `OBSIDIAN_VAULT_PATH` from env + optional
      `OBSIDIAN_EXCLUDE_PATTERNS` / `OBSIDIAN_DEFAULT_REGION`
      — `TestSetup::test_setup_persists_vault_path_from_env`
- [x] Setup raises when `OBSIDIAN_VAULT_PATH` is not set
      — `TestSetup::test_setup_requires_vault_path_env`
- [x] **import_vault**: walks markdown files recursively and submits one
      `write_seed("obsidian", ...)` per note
      — `TestImportVault::test_imports_all_markdown_notes`
- [x] **import_vault**: frontmatter `title` / `tags` flow through to the
      seed payload
      — `TestImportVault::test_passes_frontmatter_to_seed_metadata`
- [x] **import_vault**: default exclude patterns drop `.obsidian/**` and
      `Templates/**`
      — `TestImportVault::test_excludes_default_dirs`
- [x] **import_vault**: caller-supplied `exclude_patterns` replaces the
      defaults
      — `TestImportVault::test_custom_exclude_patterns`
- [x] **import_vault**: `region` kwarg overrides `default_region` from the
      binding config; absent kwarg falls back to config; absent config
      falls back to `"imported"`
      — `TestImportVault::test_region_routed_into_seed` +
      `test_region_falls_back_to_config_default`
- [x] **import_vault**: `vault_path` falls back to binding config when the
      kwarg is omitted; missing entirely raises `PluginRunError`
      — `TestImportVault::test_missing_vault_path_in_args_falls_back_to_config`
      + `test_vault_path_missing_entirely_raises`
- [x] **import_vault**: missing `context.knowledge` raises `PluginRunError`
      — `TestImportVault::test_no_knowledge_backend_raises`
- [x] **import_vault**: emits a structlog event named
      `audit.knowledge.imported.obsidian` carrying `vault_path` / `region`
      / counts (notes / scanned / skipped)
      — `TestImportVault::test_emits_audit_log_with_counts`
- [x] **Parser**: YAML frontmatter parses into a dict; body separated
      correctly
      — `test_parser.py::TestParseNote::test_parses_yaml_frontmatter_and_body`
- [x] **Parser**: missing / empty / unterminated / malformed / non-dict
      frontmatter degrades to `({}, full_text)` so a bad note can never
      blow up a batch
      — `test_parser.py::TestParseNote::test_*`
- [x] **Scanner**: missing root raises `FileNotFoundError`; root that is a
      file raises `NotADirectoryError`; non-markdown files are skipped
      — `test_client.py::TestVaultScanner::test_missing_vault_root_raises`
      + `test_vault_root_is_file_raises` + `test_skips_non_markdown`

## Manual founder dogfood

The automated suite covers the wire; the founder step is the real
host-filesystem walk:

```bash
export OBSIDIAN_VAULT_PATH=/Users/blasin/Documents/Obsidian/MyVault
# Setup binding via the obsidian plugin's setup hook OR the PWA
# Settings → Connectors page (binding flow is connector-agnostic post-Lift R1).
# Then via Direct text in the PWA:
#   "Import my Obsidian vault. Skip the templates folder."
# Workflow agent dispatches to obsidian.import_vault → seeds land in
# seeds/obsidian/ → IngestCompiler's next compile_batch promotes them to
# garden notes.
```
