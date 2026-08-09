# Google Drive backups

sub2gen can upload private database and persistent browser-profile backups to a personal Google Drive account. Google Drive is backup storage only; do not use it as the live SQLite or Chromium profile filesystem.

## Google Cloud setup

1. Create or select a Google Cloud project.
2. Enable the Google Drive API.
3. Configure the OAuth consent screen for the personal Google account that owns the backups.
4. Create an OAuth client with application type **Web application**.
5. Add this exact authorized redirect URI:

   `https://admin-flow.prismacreative.online/api/admin/backups/google-drive/oauth/callback`

6. Configure these Railway variables on the sub2gen service:

   - `SUB2GEN_GOOGLE_DRIVE_CLIENT_ID`
   - `SUB2GEN_GOOGLE_DRIVE_CLIENT_SECRET`
   - `SUB2GEN_GOOGLE_DRIVE_REDIRECT_URI`

The redirect URI variable must exactly match the URI registered in Google Cloud. Redeploy after changing Railway variables.

## Connecting and scheduling

Open **System Settings → Google Drive backups**, select **Connect Google Drive**, approve the requested `drive.file` access, and run **Test connection**. sub2gen creates a private `sub2gen Backups` folder that contains only files created by this integration.

Connecting does not automatically enable scheduled backups. Enable them separately and save the schedule. The default is daily at 03:00 `Asia/Karachi`, retaining 14 automatic backups. Manual and pre-restore safety backups are not removed by automatic retention.

## Security and restore behavior

Archives contain the SQLite database, Flow tokens, Google login cookies, local browser storage, and normal profile caches. They are compressed but are not client-side encrypted. Keep the Drive account and backup folder private.

The OAuth refresh token is stored separately on the mounted volume with restricted file permissions and is excluded from archives. BrowserMetrics, `.part` files, and Chromium `Singleton*` runtime locks are also excluded.

A full restore requires a recently authenticated admin session and the explicit confirmation text `RESTORE`. sub2gen uploads a safety backup first, downloads and validates the selected archive, checks every checksum and path, closes browser runtimes, and applies the database and profiles with rollback copies.
