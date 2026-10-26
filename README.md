# Media Manager

Media Manager is a planned self-hosted app for validating and overseeing a media automation pipeline.

## Status

Planning. This repository currently captures the project scope and initial design decisions. Application code will follow after the existing MediaValidator and VPN watchdog behavior has been reviewed.

## Planned capabilities

- Validate media and keep a history of processing and quarantine actions.
- Integrate with Radarr and Sonarr.
- Provide a web interface for media workflow and service status.
- Show VPN safety status and provide a guarded recovery flow for the download stack.

## Safety design

- Run VPN monitoring as a separate watchdog service so it remains active if the web app stops.
- Fail closed: stop the download stack when VPN health checks fail, and require manual recovery.
- Keep Docker control narrowly scoped; the web app should not receive unrestricted access to the Docker socket.
- Before recovery, verify that the VPN is healthy, its public IP differs from the normal WAN IP, and health has remained stable before starting services.

## Initial milestones

1. Review the current MediaValidator and Unraid watchdog behavior.
2. Document the monitored containers, health checks, lockout behavior, and recovery sequence.
3. Define the boundary between the watchdog and the web app.
4. Implement the watchdog and VPN Safety interface, then connect them to the existing media workflow.

No credentials, private network addresses, or deployment configuration are included in this repository.
