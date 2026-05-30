# Monitoring and alarm emails

QR Access continuously watches every configured door and emails the
building's admins the moment something stops working: a camera goes
dark, a Shelly relay becomes unreachable, or both. When the door
heals, admins get a follow-up "recovered" email so they know the
issue is closed without having to check the dashboard.

The feature is opt-in (off by default), configured per deployment in
**Settings → Monitoring**, and survives container restarts and device
reboots.

## What gets checked

Every **60 seconds**, for each door defined in **Settings → Doors**:

| Component | Check | Counts as failed when... |
|---|---|---|
| **Camera** | Has the detector received a fresh frame? | No frame in the last 60 seconds (RTSP feed down, camera unplugged, network blip, detector hung) |
| **Shelly relay** | Single RPC probe against `/rpc/Switch.GetStatus` | Any error: connection refused, timeout, HTTP 4xx/5xx, auth failure |

Doors without a Shelly configured are still monitored on the camera
side. Doors without a camera configured are skipped on both legs.

## Configuring it

1. Open **Settings → Monitoring**.
2. Turn **Enable monitoring** on.
3. Add one or more **admin emails**. Each address receives its own
   copy of every alarm (no group CC). You can add or remove addresses
   at any time - changes take effect on the next 60-second tick.
4. (Optional) Click **Send test alarm** to confirm the emails are
   reaching everyone. This sends a clearly-marked test message, not a
   real alarm.

Monitoring uses the same email provider you've configured in
**Settings → Email** (SMTP or AgentMail). If email isn't configured
yet, monitoring still tracks door health internally - it just can't
send mail until you wire up a provider.

## What the emails look like

### Alarm email

> **Subject:** `[Alarm] Door "Front Gate" - camera unreachable`
>
> ```
> Door:   Front Gate
> Time:   2026-05-30 14:32:11 (UTC)
> Issue:  No camera frames received in 73 seconds. Relay OK.
>
> Status:
>   Camera   STALE (last frame 73s ago)
>   Relay    OK
> ```

If both the camera and the relay are down at the same tick, you get a
single combined email per door (subject `... camera + relay
unreachable`), not two.

Each unhealthy door produces its own email, so a building with two
broken doors generates two alarm emails (and the rest of the fleet
stays quiet).

You will only receive one alarm per door per incident - once the
alarm is active, the monitor stays quiet until the door recovers.
No spamming every 60 seconds while a door is down.

### Recovery email

> **Subject:** `[Recovered] Door "Front Gate" - back online`
>
> ```
> Door:   Front Gate
> Time:   2026-05-30 14:38:47 (UTC)
> Status:
>   Camera   OK
>   Relay    OK
> ```

A recovery email is sent the first time a door reports healthy after
a period of being unhealthy. If the door has been healthy all along,
no email is sent.

## Boot grace period

When the qr-access service starts up - whether after a deploy, a
container restart, or a full device reboot - the monitor waits
**180 seconds** before raising any alarm. This avoids false positives
while the detector loads the YOLO model, opens RTSP streams, and
warms up on the GPU.

Door health *is* still tracked during the grace period (so a recovery
email can land naturally if a previous-incident door comes back).
Only new alarm emails are suppressed.

## State persistence

Per-door health and "is an alarm currently active" flags live in the
same SQLite database as users and access logs (`config/qr_access.db`,
inside the backed-up `config` volume). Concrete consequences:

- **Container restart mid-alarm**: the alarm flag is restored from
  disk - admins don't get re-paged for the same incident.
- **Container restart after recovery**: the cleared flag is restored
  too - no spurious "recovered" email at startup.
- **Device re-provision from a backup**: monitoring config (admin
  emails, enable/disable) is preserved along with the rest of the
  app's settings.

## When you'd want this

- Camera went offline overnight and residents discover it at 7am.
- Shelly's WiFi dropped after a router reboot and the door silently
  stopped responding to QR scans.
- The RTSP stream froze (camera firmware bug) but the camera itself
  still pings - so the detector receives no new frames even though
  the device looks up.

Monitoring catches all three within about a minute, and tells you
the moment things heal.
