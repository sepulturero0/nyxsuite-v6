# NyxSuite v6 Release and SnapBoard Password Design

## Goal

Ship the current working NyxSuite feature set as the v6 line in one public GitHub repository, `sepulturero0/nyxsuite-v6`, and make Nyxify signup use each SnapBoard row's Password column instead of a single hard-coded password.

## Scope

- Rebrand source-visible v5/v4 labels to v6 where users, builders, or future agents see them.
- Keep both Windows and macOS launch/update paths working from the current source-based packaging model.
- Use one public GitHub repo for source and releases. The in-app updater will read GitHub Releases from that same repo.
- Add release documentation so future agents know how to produce a correct update release.
- Preserve the current working tree's feature set when publishing; these changes are treated as the source state for v6.

## Architecture

The existing update system already checks GitHub Releases through `update_config.json` and applies source-based ZIP updates to `core/`, `webui/`, extension folders, scripts, root launchers, and data defaults. v6 keeps that architecture, but changes the configured repository to `sepulturero0/nyxsuite-v6`.

The existing SnapBoard-to-Nyxify pipeline is extended with a `password` field:

1. `nyxify_extension/content.js` extracts the Password column using header aliases.
2. `nyxify_extension/background.js` preserves and flushes `password` with each detected row.
3. `core/nyxify_local_api.py` accepts `password` in `/queue/upsert`.
4. `core/nyxify_task_store.py` stores the password in SQLite with a migration for existing DBs.
5. `nyxify_runner.py` passes the task password into `core/signup_flow.py`.
6. `core/signup_flow.py` fills `#password` with the row password, falling back to `ABC123wgmi*` only when the row is blank.

## Compatibility

- Existing task databases migrate in place by adding a `password` column with an empty default.
- Existing SnapBoard rows without a password keep working through the default fallback.
- Windows PowerShell and macOS/Linux shell release scripts both generate `update_config.json` pointing at the v6 public repo.
- Native-messaging manifests remain portable in release ZIPs; the checked-in manifest should not keep a machine-specific path.

## Testing

- Unit tests cover task-store password persistence and update.
- Text/structure tests cover extension password extraction/flush and v6 release config strings.
- Signup tests cover passing a row password into the signup form without logging the raw value.
- Packaging tests ensure the release ZIP contains the v6 updater config and portable native-host manifest.

## Publishing

After implementation and verification, create the public GitHub repo `sepulturero0/nyxsuite-v6` if it does not exist, set it as `origin`, commit the intended working tree, push source to the default branch, and use the same repo for future `gh release create` / `gh release upload` update assets.
