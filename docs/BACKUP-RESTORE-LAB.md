# Backup & Restore Lab

This is an experimental, local feature. Open **Backups** in SwitchAgent to copy DBI saves, export individual installed game packages, download or import a SwitchAgent ZIP, and prepare a save restore. A successful file transfer does not prove that the game will load the restored progress.

## Try the workflow without a console

Run `.\.venv\Scripts\python.exe -m switchagent.cli web --mock --port 8791` from the repository root, then open `http://127.0.0.1:8791/backups`. Choose a mock Switch, scan saves, select one or all, and create local copies. Under **Local copies**, select copies to download a ZIP. Import that ZIP to exercise archive validation. The mock restore flow lets you choose an existing save, create a safety copy, review a one-use plan, confirm, and check the result without touching physical hardware. The **Games** tab scans and exports individual mock packages.

## Read-only backup on a real Switch

Start DBI's MTP responder and connect the Switch. Close the game before copying its save. In **Backups**, choose the connected Switch, scan saves, and select one, several, or all available saves. DBI exposes saves under Installed games and Uninstalled games; both groups can be copied. The list is shallow so it remains responsive on complex saves. An unknown size or file count is shown as unknown; the actual snapshot still checks and copies the full nested tree. A ready local copy can be downloaded as a ZIP. Keep an independent copy of the ZIP outside SwitchAgent's local data directory.

The **Games** tab exports the installable packages DBI exposes. Each listed package is copied separately; this is not a full-console image, and one package is not necessarily a complete game with every update and DLC. It does not restore or install games automatically.

## First real restore: user-operated sacrificial test only

The implementation and mock tests do not qualify DBI's physical write semantics. The first write on a real Switch should be performed by the owner on an unneeded save, after keeping an independent backup. Do not use important progress for this first test.

1. Close the game. Copy the source save and download its ZIP. Verify that the ZIP is stored safely. Scan the target Switch's saves.
2. Import the SwitchAgent ZIP if needed. Choose one local copy and an **existing** target save folder of the same game on the **same** console. The target must remain a profile save; System, Device, BCAT, Cache, Temporary and other save classes cannot be substituted.
3. Select **Check restore and make safety copy**. The service checks the source, target, console fingerprint and session, then creates a ready snapshot of the target **before** offering confirmation. Review the target profile and safety-copy ID in the dialog.
4. Confirm the restore. For a different target profile, type that profile's name exactly; the server checks it. The plan is one-use and expires. Immediately before the first write, the service rechecks the source and target. Do not close SwitchAgent or disconnect the Switch during a running restore. There is no automatic retry or Shell fallback.
5. After transfer, open the game and verify the progress yourself. If the app reports an unknown outcome or a partial write, preserve the safety copy and inspect the target before doing anything else. Do not blindly retry.

Restore writes through DBI MTP/WPD only inside the chosen existing save folder. The code may remove obsolete files, create subfolders and replace files in that folder. Mock tests cover these operations, but physical DBI delete/create/commit and in-game loading remain unverified until the owner's sacrificial test.

## Limits and identity

DBI does not provide verified title ID, local user ID, or environment ID for every save. An unknown reported type can be backed up. For restore, the service classifies the source and target from their DBI save paths and refuses a change of class or a contradictory reported type; it does not invent a verified user ID. A SwitchAgent ZIP contains a device fingerprint and file hashes. The fingerprint helps prevent accidentally choosing an honest archive from a different console; **it is not signed** and does not prove origin if someone edits the ZIP. Game names, cover art and similar-looking profiles are display aids, never authority to bypass server checks. Covers may be absent because real DBI save folder names do not reliably expose a title ID.

Only one MTP operation runs at a time in the existing worker. A pending restore plan reserves its device and pauses installs until it is confirmed, cancelled or expires. Large or deeply nested saves can take time; a shallow list is no speed guarantee for the full copy. ZIP import is streamed to a local partial file and validated before it becomes a ready copy. Keep enough free disk space for the imported ZIP, snapshots and the mandatory target safety copy.
