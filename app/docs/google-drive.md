# Share to Google Drive: setting it up

Atlas can upload a movie, an episode or a clip to **your own** Google Drive and give you a link that only
people who have it can open. Nobody can find it by searching, and viewers don't need a Google account.

Atlas is open source, so it can't ship a private Google key. Instead you create your own free "OAuth client"
once (about 5 minutes) and paste its ID and secret into Atlas. It only lets Atlas see the files Atlas
uploads, nothing else in your Drive.

## One-time setup

1. Go to <https://console.cloud.google.com/> and sign in with the Google account whose Drive you want to use.
2. **Create a project**: project picker (top bar) > *New project*. Name it e.g. `Atlas`, then *Create* and
   select it.
3. **Enable the Drive API**: *APIs & Services > Library*, search for **Google Drive API**, click *Enable*.
4. **OAuth consent screen**: *APIs & Services > OAuth consent screen* (in newer consoles: *Google Auth Platform >
   Branding / Audience*).
   - User type: **External**. App name: `Atlas`; support and developer email: yours.
   - Scopes: you can skip this page (Atlas asks for `.../auth/drive.file` itself).
   - Publishing status: leave it in **Testing**, and under *Test users* add **your own Google address**.
5. **Create the client**: *APIs & Services > Credentials > Create credentials > OAuth client ID*.
   - Application type: **Desktop app**. Name: `Atlas`. *Create*.
   - Copy the **Client ID** and **Client secret** shown.
6. In Atlas: *Settings > Sharing*, paste both, then click **Connect Google Drive**. Your browser opens Google's
   sign-in; pick your account and allow access. Google warns that the app "hasn't been verified": that's expected
   for your own testing-mode app; click *Continue*.

The client secret is stored encrypted on this PC. For desktop apps Google doesn't treat it as truly secret; the
sign-in itself is protected with PKCE.

**Testing mode note:** while the consent screen is in *Testing*, Google expires the sign-in after 7 days, so
Atlas will ask you to connect again about once a week. To avoid that, set the publishing status to
*In production* (Google may show the "unverified app" warning; for an app only you use, no review is needed for
the `drive.file` scope).

## Using it

- **Share** on a title's details page, an episode's menu, or the clip result screen. Atlas uploads the file into
  an **Atlas Shares** folder in your Drive (subtitles next to the video go with it, in a folder for that title),
  then copies the link and shows a QR code to open it on a phone.
- Uploads run in the background at low priority, continue after a network drop, and pick up where they left off
  when Atlas is restarted. You can cap the upload speed in *Settings > Sharing*.
- Sharing the same file again gives you the same link instead of uploading it again.
- **Link expires**: Atlas first asks Google to expire the link. Google only supports that for some account
  types; when it doesn't, Atlas turns the link off itself at that time. That only happens while Atlas is
  running; if it's closed at that moment, the link stays open until Atlas next starts.
- **Stop sharing** turns the link off (the file stays in your Drive). **Delete from Drive** removes the files
  permanently.
- **Disconnecting** revokes Atlas's access at Google. Links you already shared keep working until you stop them
  or delete the files (you can also do that in Drive itself).

## Limits and known Google behaviour

- **Space**: free Google accounts have 15 GB shared across Drive, Gmail and Photos. Atlas checks the free space
  before uploading and tells you if the file doesn't fit.
- **"Download quota exceeded"**: if a large file is opened or downloaded by many people in a short time, Google
  may temporarily block downloads with a "Sorry, you can't view or download this file at this time" message. This
  is a Google Drive limit, not something Atlas controls; it usually clears within 24 hours.
- **Big videos may not play in the browser preview**: Drive previews are limited (very large or unusual files may
  show "still processing" for a while, or only offer a download). The file is still there; viewers can download it.
- **Copyrighted content**: Google may restrict or remove files that it identifies as infringing. Only share things
  you have the rights to share.

## Troubleshooting

| Message | What to do |
|---|---|
| "Add your Google OAuth client ID first" | Do the setup above and paste the ID and secret. |
| "Google rejected the OAuth client ID/secret" | Re-copy both from *Credentials*; the client type must be *Desktop app*. |
| "The Google Drive API isn't enabled" | Step 3. |
| "Error 403: access_denied" in the browser | Add your address under *Test users* (step 4). |
| "Google sign-in has expired" | Click *Connect Google Drive* again (weekly while in testing mode). |
| "Not enough space on Google Drive" | Free space in Drive and empty its trash. |

## For packagers

A build can embed a default client so users skip the setup: set `DEFAULT_CLIENT_ID` / `DEFAULT_CLIENT_SECRET` in
`marquee/share.py`, or the environment variables `MARQUEE_GDRIVE_CLIENT_ID` / `MARQUEE_GDRIVE_CLIENT_SECRET`.
Values entered in Settings always win. A client used by the public needs a verified consent screen.
