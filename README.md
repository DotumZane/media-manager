# Media Manager

Media Manager is a self-hosted web interface for configuring and overseeing the existing MediaValidator workflow.

## Current status

The repository now includes the MediaValidator source, its container image, and a first-run setup page. The validator continues to watch the Movies and TV folders under the completed-downloads path, scan files with ClamAV, check video streams with ffprobe, reject risky extras, stage approved media in quarantine, and request imports from Radarr or Sonarr.

The setup page lets you configure container paths, ClamAV, scan timing, Radarr, and Sonarr. It can check folder access and service connections before you save. The current validation rules remain unchanged.

## Run the container

Build the image from this folder, then run it on the same Docker network as ClamAV, Radarr, and Sonarr. Mount persistent storage at /state, the completed-downloads folder at /data/Downloads/Complete, and the quarantine folder at /data/Quarantine. ClamAV must see the same media files at the path configured as its scan root (default /scan).

Set WEB_PASSWORD in the container configuration before opening the setup page. The page uses HTTP Basic Authentication with username admin. Map host port 18473 to container port 8080, then open http://<server>:18473 from your trusted home network. The app can keep listening on container port 8080, so this avoids a conflict with another container using host port 8080.

## Credentials and Git

- Radarr and Sonarr API keys are accepted through the setup page and stored in /state/config.json with owner-only file permissions.
- Saved API keys are never returned to the browser. Leave a key field blank to keep the saved key; use its remove checkbox to clear it.
- .gitignore excludes local environment files, runtime configuration, state folders, and database files. Do not add /state/config.json, .env, or deployment-specific secrets to Git.
- The source reads RADARR_API_KEY and SONARR_API_KEY from environment variables for compatibility, but contains no key values.
- The setup UI uses HTTP. Keep it on a trusted local network; use HTTPS through a protected proxy for remote access.

## Existing settings

The setup page exposes the validator's current configurable values:

- Completed-downloads and quarantine paths.
- ClamAV host, port, and shared scan path.
- Scan interval, file-settle time, and Radarr/Sonarr import retry interval.
- Radarr and Sonarr URLs, API keys, and their import paths.

Initial defaults are based on the existing validator, with a generic clamav hostname instead of a private network address. The state database remains under /state/validator.db.
