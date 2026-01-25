# CivicMesh Invariants

- Only public, configured channels are visible or writable in v0.
- Captive portal UI must load even if the radio is disconnected.
- Messages are never deleted before pruning rules allow it.
- Outbound message state transitions are monotonic.
- sent-to-radio never implies delivery to recipients.
- Rate limiting is enforced before enqueue.
- Input validation rejects empty or oversized messages.
- All outbound messages are persisted before any send attempt.
- Outbox retries must back off (no tight loops).
- Cached messages remain readable during radio outages.
- No HTTPS or account features are added in v0.
- No direct internet dependencies at runtime.
- Security log must not include full message content.
- DB schema changes preserve existing data or include migration.
- CPU/RAM usage remains suitable for Pi Zero 2W.
