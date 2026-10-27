# Media Manager

Media Manager is a self-hosted web interface for configuring and overseeing the existing MediaValidator workflow.

## Current status

The repository includes the MediaValidator source, a guided setup page, an Unraid Community Apps template, and a GitHub Actions workflow that publishes the container image to GitHub Container Registry. The Community Apps catalog listing still needs to be submitted by the maintainer at https://ca.unraid.net/submit/new.

The validator watches the Movies and TV folders under the completed-downloads path, scans files with ClamAV, checks video streams with ffprobe, rejects risky extras, stages approved media in quarantine, and requests imports from Radarr or Sonarr. The setup page configures folders, ClamAV, scan timing, Radarr, and Sonarr without changing the existing validation rules.

## Install on Unraid

After the Community Apps submission is accepted, search for Media Manager in the Unraid Apps tab and install it.

The template maps host port 18473 to the container's WebUI on port 8080. It also creates a persistent appdata mapping at /mnt/user/appdata/media-manager to /state. Keep that mapping in place during updates so the settings and validator history remain available. When replacing an existing MediaValidator container, point /state at the same host folder the old container used.

Set the Media Data Root mapping to the host folder that contains both Downloads/Complete and Quarantine. The completed-downloads folder must contain Movies and TV directories. Keeping both folders under one container mount lets the validator hardlink approved files. ClamAV must see those same media files at its configured scan path, which defaults to /scan.

Set a unique WEB_PASSWORD in the template. Open http://<Unraid-IP>:18473 and sign in with username admin and the password you set. The setup page can check paths and service connections before saving.

## Container image

The image is published as ghcr.io/dotumzane/media-manager:latest. The workflow also publishes commit-tagged images for rollback and pinning.

## Credentials and Git

- Radarr and Sonarr API keys are entered through the setup page and stored in /state/config.json with owner-only file permissions.
- Saved API keys are never returned to the browser. Leave a key field blank to keep a saved key; use its remove checkbox to clear it.
- .gitignore and .dockerignore exclude local environment files, runtime configuration, state folders, and database files. Do not commit /state/config.json, .env, or deployment-specific secrets.
- The source reads RADARR_API_KEY and SONARR_API_KEY from environment variables for compatibility, but contains no key values.
- The setup UI uses HTTP. Keep it on a trusted local network; use HTTPS through a protected proxy for remote access.

## Existing settings

The setup page exposes the validator's current configurable values:

- Completed-downloads and quarantine paths.
- ClamAV host, port, and shared scan path.
- Scan interval, file-settle time, and Radarr/Sonarr import retry interval.
- Radarr and Sonarr URLs, API keys, and their import paths.

The public source uses the generic ClamAV hostname clamav instead of publishing the prior private network address. If your setup uses another host, set it in the setup page. The state database remains at /state/validator.db.
