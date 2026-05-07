# MeshCore Protocol Reference

A technical reference for the MeshCore LoRa mesh networking protocol, compiled
for the **CivicMesh** project. This document covers the wire protocol,
cryptography, routing, the companion serial protocol, and the over-the-air
request flows used for status polling and telemetry.

**Sources** (primary, authoritative):

- `MeshCore/docs/packet_format.md`
- `MeshCore/docs/payloads.md`
- `MeshCore/docs/companion_protocol.md`
- `MeshCore/docs/stats_binary_frames.md`
- `MeshCore/docs/qr_codes.md`
- `MeshCore/docs/number_allocations.md`
- `meshcore_py` source (`packets.py`, `events.py`, `parsing.py`, `commands/`)

Where this doc and the firmware docs disagree, the firmware docs win — file an
update against this doc.

---

## Table of Contents

1. [Overview](#1-overview)
2. [Node Types](#2-node-types)
3. [Radio Configuration](#3-radio-configuration)
4. [Packet Structure](#4-packet-structure)
5. [Payload Types](#5-payload-types)
6. [Cryptography](#6-cryptography)
7. [Channel System](#7-channel-system)
8. [Routing](#8-routing)
9. [Companion Serial Protocol](#9-companion-serial-protocol)
10. [Over-the-Air Request Flows](#10-over-the-air-request-flows)
11. [Telemetry & Cayenne-LPP](#11-telemetry--cayenne-lpp)
12. [Admin Operations for CivicMesh](#12-admin-operations-for-civicmesh)
13. [Practical Recipes](#13-practical-recipes)
14. [QR Codes](#14-qr-codes)
15. [Hardware Notes](#15-hardware-notes)
16. [Resources & Quick Reference](#16-resources--quick-reference)

---

## 1. Overview

MeshCore is a LoRa-based mesh networking protocol designed for off-grid text
communication and low-rate sensor data. Key characteristics:

- **Encrypted by default.** AES-128-ECB for channels and DMs; Ed25519
  signatures for adverts.
- **Decentralized.** No central infrastructure; peer-to-peer.
- **Flood routing for channels.** All channel messages propagate through every
  repeater that can hear them.
- **Path learning for DMs.** Direct messages flood the first time, then learn
  a route via a returned PATH packet.
- **No delivery guarantee for channels.** Fire-and-forget; ACKs are a
  DM-only feature.
- **Low bandwidth.** Roughly 1–2 kbps total airtime shared across the
  contiguous mesh, depending on radio settings.

### What MeshCore is NOT

- Not store-and-forward across the mesh: if the recipient is offline when a
  message floods past, they will not get it. (Companion radios buffer for the
  paired client only.)
- Not protocol-rate-limited: abuse mitigation is social, not technical.
- Not an internet replacement: very low throughput, high latency.

---

## 2. Node Types

### 2.1 Companion Radio

A radio that pairs with a phone, computer, or single-board computer over BLE,
USB serial, or TCP. The companion:

- Handles all LoRa TX/RX.
- Buffers received messages locally for the connected client.
- Exposes a binary command interface to the client (see §9).
- Maintains the contact list and channel slots.

**Common hardware:** Heltec V3, RAK4631, T1000-E.

### 2.2 Repeater

A standalone node that rebroadcasts valid packets. Repeaters:

- Don't need to be subscribed to channels they relay.
- Can run silently for months — no adverts required.
- Build up routing paths as packets traverse them.
- Can be administered over LoRa via a password-gated login (see §10.2).

### 2.3 Room Server

A specialized repeater that hosts persistent "rooms" (group chats):

- Requires login with a password.
- Stores message history.
- Pushes stored messages to logged-in clients.

### 2.4 Sensor

A node that publishes readings as Cayenne-LPP-encoded telemetry (see §11).
Sensor nodes can be polled with a binary `TELEMETRY` request (§10.3) and may
also expose min/max/average aggregate data (`MMA`).

### 2.5 Standalone Clients

Devices with built-in UI that don't need a companion:

- **T-Deck** — full keyboard.
- **T-Pager** — pager form factor.

---

## 3. Radio Configuration

All nodes in a contiguous mesh must share identical radio settings.

### 3.1 Parameters

| Parameter | Description | Common Values |
|-----------|-------------|---------------|
| Frequency | Center frequency in MHz | 910.525 (US), 869.525 (EU) |
| Bandwidth (BW) | Channel width in kHz | 62.5, 125, 250, 500 |
| Spreading Factor (SF) | LoRa spreading factor | 7–12 (higher = longer range, slower) |
| Coding Rate (CR) | Forward error correction | 5–8 (5 = 4/5, 8 = 4/8) |

### 3.2 PNW / Seattle Settings (as of late 2025)

```
Frequency: 910.525 MHz
Bandwidth: 62.5 kHz
Spreading Factor: SF7
Coding Rate: CR5
```

These were chosen to avoid smart-meter interference that affected the older
SF11/BW125 settings.

### 3.3 Tradeoffs

| Setting | Effect |
|---------|--------|
| Higher SF | Longer range, slower data rate, more airtime |
| Lower BW | Longer range, slower data rate |
| Higher CR | More error resilience, slower data rate |

---

## 4. Packet Structure

### 4.1 Wire Format

Every MeshCore packet has this structure:

```
+--------+-------------------+-------------+---------+----------+
| header | transport_codes   | path_length | path    | payload  |
| 1 byte | 4 bytes (optional)| 1 byte      | 0–192 B | 0–184 B  |
+--------+-------------------+-------------+---------+----------+
```

`transport_codes` is only present when `route_type` is `TRANSPORT_FLOOD` or
`TRANSPORT_DIRECT`.

### 4.2 Header Byte

```
Bit:  7  6  5  4  3  2  1  0
      |  |  |  |  |  |  |  |
      |  |  |  |  |  |  +--+--  Route Type   (bits 0–1, mask 0x03)
      |  |  +--+--+--+--------  Payload Type (bits 2–5, mask 0x3C)
      +--+--------------------  Payload Ver  (bits 6–7, mask 0xC0)
```

**Route Types (bits 0–1):**

| Value | Name | Description |
|-------|------|-------------|
| `0x00` | `ROUTE_TYPE_TRANSPORT_FLOOD` | Flood + transport codes |
| `0x01` | `ROUTE_TYPE_FLOOD` | Flood routing, path built up as packet traverses |
| `0x02` | `ROUTE_TYPE_DIRECT` | Direct routing, path supplied by sender |
| `0x03` | `ROUTE_TYPE_TRANSPORT_DIRECT` | Direct + transport codes |

**Payload Types (bits 2–5):**

| Value | Name | Description |
|-------|------|-------------|
| `0x00` | `REQ` | Request (encrypted, dst+src hashes + MAC) |
| `0x01` | `RESPONSE` | Response to `REQ` or `ANON_REQ` |
| `0x02` | `TXT_MSG` | Plain-text DM |
| `0x03` | `ACK` | Acknowledgment |
| `0x04` | `ADVERT` | Node advertisement |
| `0x05` | `GRP_TXT` | Group/channel text message |
| `0x06` | `GRP_DATA` | Group/channel datagram |
| `0x07` | `ANON_REQ` | Anonymous request (sender pubkey is in payload) |
| `0x08` | `PATH` | Returned path |
| `0x09` | `TRACE` | Path trace, collecting SNR per hop |
| `0x0A` | `MULTIPART` | Fragment of a multi-packet sequence |
| `0x0B` | `CONTROL` | Unencrypted control/discovery |
| `0x0C–0x0E` | reserved | — |
| `0x0F` | `RAW_CUSTOM` | Custom application payload |

**Payload Version (bits 6–7):**

| Value | Description |
|-------|-------------|
| `0x00` | v1: 1-byte src/dest hashes, 2-byte MAC |
| `0x01–0x03` | reserved for future versions |

### 4.3 Header Examples

```python
# GRP_TXT (channel msg) with FLOOD routing, version 1
# payload_type=5, route_type=1, version=0
header = (5 << 2) | 1   # = 0x15

# ADVERT with FLOOD routing
header = (4 << 2) | 1   # = 0x11

# ACK with DIRECT routing
header = (3 << 2) | 2   # = 0x0E
```

### 4.4 path_length Byte (IMPORTANT)

`path_length` is **not** a raw byte count. It packs both the hash size and the
hop count:

| Bits | Field | Meaning |
|------|-------|---------|
| 0–5 | hop_count | Number of path hashes (0–63) |
| 6–7 | hash_size_code | Hash size minus 1 |

**Hash size codes:**

| Bits 6–7 | Hash size | Notes |
|----------|-----------|-------|
| `0b00` | 1 byte | Legacy / default |
| `0b01` | 2 bytes | Supported in current firmware |
| `0b10` | 3 bytes | Supported in current firmware |
| `0b11` | reserved | Invalid |

**Examples:**

| `path_length` | Meaning | Path bytes |
|---------------|---------|------------|
| `0x00` | zero hops, no path bytes | 0 |
| `0x05` | 5 hops × 1-byte hashes | 5 |
| `0x45` | 5 hops × 2-byte hashes | 10 |
| `0x8A` | 10 hops × 3-byte hashes | 30 |

```python
def parse_path_length(b: int) -> tuple[int, int]:
    """Returns (hop_count, hash_size_bytes)."""
    hop_count = b & 0x3F
    hash_size = ((b >> 6) & 0x03) + 1
    return hop_count, hash_size
```

For flood packets, the path is built up as the packet traverses repeaters.
For direct packets, the path is supplied by the sender and consumed hop by
hop. Maximum: 64 hops; effective path bytes = `hop_count * hash_size`.

A node's hash is the first `hash_size` bytes of its Ed25519 public key.

---

## 5. Payload Types

### 5.1 Node Advertisement (ADVERT, `0x04`)

Announces a node's existence and capabilities.

```
+------------+-----------+-----------+----------+
| public key | timestamp | signature | appdata  |
| 32 bytes   | 4 bytes   | 64 bytes  | variable |
+------------+-----------+-----------+----------+
```

The signature is Ed25519 over `public_key || timestamp || appdata`.

**Appdata:**

```
+-------+----------+-----------+-----------+-----------+----------+
| flags | latitude | longitude | feature 1 | feature 2 | name     |
| 1 B   | 4 (opt)  | 4 (opt)   | 2 (opt)   | 2 (opt)   | UTF-8    |
+-------+----------+-----------+-----------+-----------+----------+
```

**Flags byte layout (lower nibble = node type enum, upper nibble = bitmask):**

| Bits | Mask | Meaning |
|------|------|---------|
| 0–3 (low nibble) | `0x0F` | Node type enum (see below) |
| 4 | `0x10` | Has location |
| 5 | `0x20` | Has feature 1 |
| 6 | `0x40` | Has feature 2 |
| 7 | `0x80` | Has name |

**Node type enum (low nibble values):**

| Value | Name |
|-------|------|
| `0x01` | Chat node (companion) |
| `0x02` | Repeater |
| `0x03` | Room server |
| `0x04` | Sensor |

**Latitude / longitude format:** signed 32-bit integers, decimal degrees ×
1,000,000. Optional — present only if the location flag is set.

### 5.2 Acknowledgment (ACK, `0x03`)

```
+----------+
| checksum |
| 4 bytes  |
+----------+
```

`checksum` is the CRC of `timestamp || text || sender_pubkey` from the
original DM. ACKs may also be bundled into a returned PATH packet's `extra`
field instead of being sent as a separate packet. **CLI commands sent over
DM do not produce ACKs.**

### 5.3 DM-Family Envelope (REQ, RESPONSE, TXT_MSG, PATH)

REQ, RESPONSE, TXT_MSG, and PATH all share an envelope:

```
+----------+----------+-----+------------+
| dst_hash | src_hash | MAC | ciphertext |
| 1 byte   | 1 byte   | 2 B | rest       |
+----------+----------+-----+------------+
```

The plaintext shape inside the ciphertext depends on payload type.

#### TXT_MSG plaintext

```
+-----------+-------------------+----------+
| timestamp | txt_type+attempt  | message  |
| 4 bytes   | 1 byte            | UTF-8    |
+-----------+-------------------+----------+
```

The byte at offset 4 is **packed**:

- Upper 6 bits: `txt_type`
- Lower 2 bits: `attempt` (retry counter, 0–3)

```python
b = plaintext[4]
txt_type = b >> 2
attempt  = b & 0x03
```

**txt_type values:**

| Value | Meaning |
|-------|---------|
| `0x00` | Plain text message |
| `0x01` | CLI command (no ACK is generated) |
| `0x02` | Signed plain text — first 4 bytes of message are the sender pubkey prefix; rest is text |

#### REQ plaintext

```
+-----------+--------------+
| timestamp | request_data |
| 4 bytes   | rest         |
+-----------+--------------+
```

`request_data[0]` is the request opcode in the chat/server profile:

| Value | Meaning |
|-------|---------|
| `0x01` | Get stats |
| `0x02` | Keep-alive |

Note: telemetry, neighbours, ACL, MMA, owner-info, and regions are not
encoded as REQ — they go through the binary-request flow over `RESPONSE` (see
§10).

#### RESPONSE plaintext

```
+----------------------+
| application response |
| rest of payload      |
+----------------------+
```

Opaque application bytes, decoded by request type.

#### Returned PATH plaintext

```
+-------------+------+------------+--------+
| path_length | path | extra_type | extra  |
| 1 byte      | N    | 1 byte     | rest   |
+-------------+------+------------+--------+
```

`path_length` follows the same encoding as the outer header path field
(§4.4). `extra_type` is one of the payload type values; the bundled `extra`
packet is most commonly an `ACK` or `RESPONSE`.

### 5.4 Anonymous Request (ANON_REQ, `0x07`)

Used when the sender wants to identify itself in the payload rather than
through a known DM relationship — including repeater logins and the
unauthenticated repeater info queries (clock/owner/regions).

```
+----------+--------------+-----+------------+
| dst_hash | sender_pubkey| MAC | ciphertext |
| 1 byte   | 32 bytes     | 2 B | rest       |
+----------+--------------+-----+------------+
```

The shared secret is derived per request from the sender's Curve25519 key and
the recipient's pubkey (see §6.1).

The ciphertext is one of these payloads:

#### Repeater / Sensor login

```
+-----------+----------+
| timestamp | password |
| 4 bytes   | UTF-8    |
+-----------+----------+
```

#### Room-server login

```
+-----------+----------------+----------+
| timestamp | sync_timestamp | password |
| 4 bytes   | 4 bytes        | UTF-8    |
+-----------+----------------+----------+
```

`sync_timestamp` is the "show me messages since" cutoff for backfill.

#### Repeater info request (no auth required)

| `req_type` | Subject | `AnonReqType` constant |
|------------|---------|------------------------|
| `0x01` | Regions | `REGIONS` |
| `0x02` | Owner info | `OWNER` |
| `0x03` | Clock + status (BASIC) | `BASIC` |

```
+-----------+----------+----------------+-------------+
| timestamp | req_type | reply_path_len | reply_path  |
| 4 bytes   | 1 byte   | 1 byte         | variable    |
+-----------+----------+----------------+-------------+
```

`reply_path` follows the same path-length encoding as the outer header
(§4.4). The repeater answers with a `RESPONSE` routed back along the supplied
path.

### 5.5 Group/Channel Message (GRP_TXT, `0x05`)

```
+--------------+-----+------------+
| channel_hash | MAC | ciphertext |
| 1 byte       | 2 B | rest       |
+--------------+-----+------------+
```

`channel_hash` = first byte of `SHA-256(channel_secret)` — see §7.2 for the
two-hop derivation.

The plaintext is the same as `TXT_MSG` plaintext (§5.3), with the
**convention** that the message body is `"<sender_name>: <text>"`. `txt_type`
is typically `0x00`. The sender name is **not cryptographically verified** —
it's just a string.

### 5.6 Group Datagram (GRP_DATA, `0x06`)

```
+--------------+-----+------------+
| channel_hash | MAC | ciphertext |
| 1 byte       | 2 B | rest       |
+--------------+-----+------------+
```

Plaintext (after MAC-then-decrypt):

```
+-----------+----------+--------+
| data_type | data_len | data   |
| 2 bytes   | 1 byte   | varies |
+-----------+----------+--------+
```

`data_type` is a 16-bit identifier from the
`MeshCore/docs/number_allocations.md` registry:

- `0x0000–0x00FF` reserved for internal use
- `0x0100–0xFEFF` allocated to specific application namespaces (PR to
  register)
- `0xFF00–0xFFFF` reserved for development/testing — no allocation needed

Applications that need a timestamp must encode it in `data` themselves.

### 5.7 Trace (TRACE, `0x09`)

A trace packet records each hop's SNR as it traverses the mesh.

```
+--------+-----------+-----------+
| tag    | auth_code | path data |
| 4 B    | 4 B       | rest      |
+--------+-----------+-----------+
```

Each entry in the path data is a node hash of width `hash_size` (taken from
the outer header's `path_length` byte) followed by a signed 1-byte SNR×4.
The final byte is the destination node's own SNR×4.

The companion surfaces parsed traces as `EventType.TRACE_DATA`.

### 5.8 Multipart (MULTIPART, `0x0A`)

A fragment of a larger logical packet. The receiver reassembles based on
sequence/header bytes.

The current public docs (`MeshCore/docs/payloads.md`) do not specify the
fragment header byte layout — when working with multipart in CivicMesh,
verify against the firmware (`Mesh.cpp` / `BaseChatMesh.cpp`) before
assuming a format.

### 5.9 Control (CONTROL, `0x0B`)

Unencrypted control plane. The first byte is `flags`; the upper 4 bits are a
sub-type.

| Sub-type | Name | Description |
|----------|------|-------------|
| `0x80` | `NODE_DISCOVER_REQ` | Solicit a directed advert |
| `0x90` | `NODE_DISCOVER_RESP` | Response with node identity + SNR |

#### `NODE_DISCOVER_REQ` payload

```
+-------+-------------+--------+----------+
| flags | type_filter | tag    | since    |
| 1 B   | 1 B         | 4 B    | 4 B (opt)|
+-------+-------------+--------+----------+
```

- `flags`: upper nibble `0x8`; bit 0 = `prefix_only`.
- `type_filter`: bitmask of `ADV_TYPE_*` values (see §5.1) the requester
  wants.
- `tag`: random, echoed back in the response.
- `since`: optional Unix timestamp; respond only if the node has changed
  since then.

#### `NODE_DISCOVER_RESP` payload

```
+-------+-----+--------+--------------+
| flags | snr | tag    | pubkey       |
| 1 B   | 1 B | 4 B    | 8 or 32 B    |
+-------+-----+--------+--------------+
```

- `flags`: upper nibble `0x9`; lower nibble = node type (1 chat / 2 repeater
  / 3 room / 4 sensor).
- `snr`: signed, ×4.
- `pubkey`: 8-byte prefix when the request set `prefix_only`, else the full
  32-byte key.

### 5.10 Custom (RAW_CUSTOM, `0x0F`)

Application-defined; no protocol structure.

---

## 6. Cryptography

### 6.1 Overview

| Message Type | Key Exchange | Cipher | MAC |
|--------------|--------------|--------|-----|
| Direct messages | ECDH (Curve25519) | AES-128-ECB | HMAC-SHA-256 (truncated to 2 bytes) |
| Channel messages | Pre-shared 16-byte secret | AES-128-ECB | HMAC-SHA-256 (truncated to 2 bytes) |
| Anonymous requests | ECDH from sender pubkey carried in payload | AES-128-ECB | HMAC-SHA-256 (truncated to 2 bytes) |
| Advertisements | Ed25519 signature | none | 64-byte signature |

### 6.2 AES-128-ECB

```python
from Crypto.Cipher import AES

def encrypt(key16: bytes, plaintext: bytes) -> bytes:
    """Pad plaintext to a 16-byte boundary with zeros, then AES-128-ECB."""
    padded_len = ((len(plaintext) + 15) // 16) * 16
    padded = plaintext.ljust(padded_len, b"\x00")
    return AES.new(key16, AES.MODE_ECB).encrypt(padded)

def decrypt(key16: bytes, ciphertext: bytes) -> bytes:
    return AES.new(key16, AES.MODE_ECB).decrypt(ciphertext)
```

Zero-padding is sufficient because the plaintext shapes are length-prefixed
or terminated structurally.

### 6.3 HMAC-SHA-256 → 2-byte MAC

The MAC is computed over the **ciphertext**, then truncated to 2 bytes.

```python
import hmac, hashlib

def compute_mac(key32: bytes, ciphertext: bytes) -> bytes:
    return hmac.new(key32, ciphertext, hashlib.sha256).digest()[:2]
```

The HMAC key is 32 bytes; the AES key is the first 16 bytes of the same
shared secret.

### 6.4 Encrypt-then-MAC

```python
def encrypt_then_mac(secret: bytes, plaintext: bytes) -> bytes:
    """Returns: MAC (2 bytes) || ciphertext."""
    ciphertext = encrypt(secret[:16], plaintext)
    return compute_mac(secret, ciphertext) + ciphertext
```

### 6.5 MAC-then-Decrypt

```python
def mac_then_decrypt(secret: bytes, data: bytes) -> bytes | None:
    """Verify MAC and decrypt. Returns plaintext or None on MAC failure."""
    if len(data) <= 2:
        return None
    mac, ciphertext = data[:2], data[2:]
    if mac != compute_mac(secret, ciphertext):
        return None
    return decrypt(secret[:16], ciphertext)
```

Truncated 2-byte MACs have a 1/65,536 false-accept rate per packet. That is
acceptable for low-rate text traffic but **not** suitable for adversarial
attacks; treat MeshCore message authentication as accidental-tamper
detection, not security.

---

## 7. Channel System

### 7.1 Channel Secret Derivation

Channels are identified by a human-readable name (e.g. `#civicmesh`). The
16-byte channel secret is derived locally and never transmitted on its own:

```python
import hashlib

def derive_channel_secret(channel_name: str) -> bytes:
    """Channel names are case-sensitive: #civicmesh != #CivicMesh."""
    return hashlib.sha256(channel_name.encode("utf-8")).digest()[:16]
```

### 7.2 Channel Hash Byte (Two-Hop)

The 1-byte `channel_hash` in `GRP_TXT` / `GRP_DATA` packets is **not** the
first byte of the secret. It's the first byte of `SHA-256` **of** the
secret:

```python
def derive_channel_hash_byte(channel_name: str) -> int:
    secret = hashlib.sha256(channel_name.encode("utf-8")).digest()[:16]
    return hashlib.sha256(secret).digest()[0]
```

This is verified against `MeshCore/docs/payloads.md` ("first byte of SHA256 of
channel's shared key").

**Example:**

```python
channel = "#civicmesh"
secret = derive_channel_secret(channel)         # ed040e9170c57b29...
hash_byte = derive_channel_hash_byte(channel)   # 0xb4

# Common mistake:
#   sha256(channel_name)[0]      -> 0xed   wrong
# Correct:
#   sha256(sha256(channel_name)[:16])[0]  -> 0xb4
```

### 7.3 Public Channel Key

The default public channel uses a publicly known 16-byte key:
`8b3387e9c5cdea6ac9e5edbaa115cd72`. Anyone can decrypt; treat its contents
as public.

### 7.4 Channel Message Encryption

```python
import struct

def encrypt_channel_message(
    channel_name: str, sender_name: str, text: str, timestamp: int
) -> bytes:
    secret      = derive_channel_secret(channel_name)
    channel_hash = derive_channel_hash_byte(channel_name)

    body = f"{sender_name}: {text}".encode("utf-8")
    plaintext = struct.pack("<I", timestamp) + b"\x00" + body  # txt_type=0, attempt=0

    encrypted = encrypt_then_mac(secret + secret, plaintext)  # any 32-byte HMAC key works
    return bytes([channel_hash]) + encrypted
```

(MeshCore firmware uses the secret itself as the HMAC key material; what
matters for interop is that both sides compute the MAC the same way.)

### 7.5 Channel Message Decryption

```python
def decrypt_channel_message(channel_name: str, payload: bytes) -> dict | None:
    if len(payload) < 3:
        return None

    channel_hash, mac_and_ciphertext = payload[0], payload[1:]
    if channel_hash != derive_channel_hash_byte(channel_name):
        return None  # not our channel

    secret = derive_channel_secret(channel_name)
    plaintext = mac_then_decrypt(secret, mac_and_ciphertext)
    if plaintext is None:
        return None  # MAC verification failed

    timestamp     = struct.unpack("<I", plaintext[0:4])[0]
    txt_type_byte = plaintext[4]
    txt_type = txt_type_byte >> 2
    attempt  = txt_type_byte & 0x03
    message  = plaintext[5:].rstrip(b"\x00").decode("utf-8", errors="replace")

    return {
        "timestamp": timestamp,
        "txt_type":  txt_type,
        "attempt":   attempt,
        "message":   message,
    }
```

---

## 8. Routing

### 8.1 Flood Routing (Channels and First-Contact DMs)

1. Sender broadcasts the packet.
2. Every repeater that hears it rebroadcasts once.
3. Loop prevention: each repeater hashes packets it's seen and ignores
   duplicates.
4. The `path` field accumulates the hops the packet traverses.

**Implications:**

- No delivery confirmation is possible — the sender doesn't know who heard
  it.
- All traffic competes for shared airtime.
- Geographic separation creates independent flood domains that can carry the
  same channel traffic without interfering.

### 8.2 Direct Routing (Established DMs)

1. **First message** floods like a channel message.
2. **Recipient replies** with a `PATH` packet that records the route the
   request took.
3. **Subsequent messages** use direct routing: the sender supplies the path
   in the packet header.
4. **Route maintenance:** paths can become stale if nodes move. A failed
   direct send can fall back to flood, or path discovery can be triggered
   explicitly.

### 8.3 Loop Prevention

Repeaters maintain a hash table of recently-seen packets:

```cpp
// from Mesh.h
virtual bool hasSeen(const Packet* packet) = 0;
virtual void clear(const Packet* packet)   = 0;
```

Packet hash = hash of `(payload + type)`. First copy wins; duplicates are
dropped.

### 8.4 Repeat Counting (Heuristic)

When you send a message, you may hear it echoed back via repeaters. This
indicates *propagation*, not delivery:

| Repeats heard | Loose interpretation |
|---------------|----------------------|
| 0 | Possibly nothing in range, or radio issue |
| 1–2 | Local propagation confirmed |
| 3+ | Good mesh penetration in your area |

Repeats heard ≠ delivery confirmation. The recipient may still be offline,
out of range, or have a radio that isn't decoding cleanly.

---

## 9. Companion Serial Protocol

The companion radio exposes a binary command/response protocol over its host
transport (USB serial, BLE, or TCP). For BLE, see
`MeshCore/docs/companion_protocol.md` for service/characteristic UUIDs and
MTU notes.

### 9.1 Frame Convention

- All multi-byte integers are **little-endian** (Cayenne-LPP fields are an
  exception — big-endian).
- Strings are UTF-8.
- Each transport frame carries exactly **one** companion protocol frame.
  Apps must validate frame lengths before parsing.
- Most frames are: `[1-byte type] [variable data]`.

### 9.2 Connection Bring-Up

On a fresh connection the client should:

1. `CMD_APP_START` (0x01) — identifies the client. Triggers `SELF_INFO`.
2. `CMD_DEVICE_QUERY` (0x16) — fetch firmware build, BLE PIN, model, radio
   settings.
3. `CMD_SET_DEVICE_TIME` (0x06) — set the firmware clock from the host.
4. `CMD_GET_CONTACTS` (0x04) — sync the contact list.
5. `CMD_GET_CHANNEL` (0x1F) for each slot (0..max_channels-1) — sync
   channels.
6. `CMD_SYNC_NEXT_MESSAGE` (0x0A) — drain any queued messages.
7. Subscribe to push notifications (§9.5) — `MESSAGES_WAITING`,
   `ADVERTISEMENT`, `ACK`, `LOG_DATA`, `TELEMETRY_RESPONSE`, etc.

### 9.3 Command Codes (`CommandType`)

The full command table is in
`meshcore_py/src/meshcore/packets.py::CommandType`. Highlights, grouped by
purpose:

**Identity / time / config**

| Code | Name |
|------|------|
| `1` | `APP_START` |
| `5` | `GET_DEVICE_TIME` |
| `6` | `SET_DEVICE_TIME` |
| `7` | `SEND_SELF_ADVERT` |
| `8` | `SET_ADVERT_NAME` |
| `12` | `SET_RADIO_TX_POWER` |
| `11` | `SET_RADIO_PARAMS` |
| `14` | `SET_ADVERT_LATLON` |
| `19` | `REBOOT` |
| `20` | `GET_BATT_AND_STORAGE` |
| `21` | `SET_TUNING_PARAMS` |
| `22` | `DEVICE_QUERY` |
| `23` | `EXPORT_PRIVATE_KEY` |
| `24` | `IMPORT_PRIVATE_KEY` |
| `37` | `SET_DEVICE_PIN` |
| `38` | `SET_OTHER_PARAMS` |
| `40` | `GET_CUSTOM_VARS` |
| `41` | `SET_CUSTOM_VAR` |
| `43` | `GET_TUNING_PARAMS` |
| `51` | `FACTORY_RESET` |

**Contacts**

| Code | Name |
|------|------|
| `4` | `GET_CONTACTS` |
| `9` | `ADD_UPDATE_CONTACT` |
| `13` | `RESET_PATH` |
| `15` | `REMOVE_CONTACT` |
| `16` | `SHARE_CONTACT` |
| `17` | `EXPORT_CONTACT` |
| `18` | `IMPORT_CONTACT` |
| `30` | `GET_CONTACT_BY_KEY` |
| `42` | `GET_ADVERT_PATH` |
| `58` | `SET_AUTOADD_CONFIG` |
| `59` | `GET_AUTOADD_CONFIG` |

**Channels**

| Code | Name |
|------|------|
| `31` | `GET_CHANNEL` |
| `32` | `SET_CHANNEL` |

**Messaging**

| Code | Name |
|------|------|
| `2` | `SEND_TXT_MSG` |
| `3` | `SEND_CHANNEL_TXT_MSG` |
| `10` | `SYNC_NEXT_MESSAGE` |
| `25` | `SEND_RAW_DATA` |
| `26` | `SEND_LOGIN` |
| `27` | `SEND_STATUS_REQ` |
| `29` | `LOGOUT` |
| `36` | `SEND_TRACE_PATH` |
| `39` | `SEND_TELEMETRY_REQ` (legacy; prefer `BINARY_REQ`) |
| `50` | `BINARY_REQ` (status / telemetry / MMA / ACL / neighbours) |
| `52` | `PATH_DISCOVERY` |
| `54` | `SET_FLOOD_SCOPE` |
| `55` | `SEND_CONTROL_DATA` |
| `57` | `SEND_ANON_REQ` |
| `63` | `SET_DEFAULT_FLOOD_SCOPE` |
| `64` | `GET_DEFAULT_FLOOD_SCOPE` |

**Stats / health**

| Code | Name |
|------|------|
| `56` | `GET_STATS` (sub-types CORE=0, RADIO=1, PACKETS=2 — see §9.6) |
| `60` | `GET_ALLOWED_REPEAT_FREQ` |
| `61` | `SET_PATH_HASH_MODE` |
| `28` | `HAS_CONNECTION` |
| `33`–`35` | `SIGN_START` / `SIGN_DATA` / `SIGN_FINISH` (offline signing) |

### 9.4 Solicited Response Codes (`PacketType`)

Companion responses to commands use a single-byte type prefix:

| Code | Name | Triggered by |
|------|------|--------------|
| `0x00` | `OK` | most write commands; may include 4-byte value |
| `0x01` | `ERROR` | any command (see error table below) |
| `0x02–0x04` | `CONTACT_START` / `CONTACT` / `CONTACT_END` | `GET_CONTACTS` |
| `0x05` | `SELF_INFO` | `APP_START` |
| `0x06` | `MSG_SENT` | `SEND_TXT_MSG`, `SEND_CHANNEL_TXT_MSG` |
| `0x07` | `CONTACT_MSG_RECV` | `SYNC_NEXT_MESSAGE` |
| `0x08` | `CHANNEL_MSG_RECV` | `SYNC_NEXT_MESSAGE` |
| `0x09` | `CURRENT_TIME` | `GET_DEVICE_TIME` |
| `0x0A` | `NO_MORE_MSGS` | `SYNC_NEXT_MESSAGE` |
| `0x0B` | `CONTACT_URI` | `EXPORT_CONTACT` |
| `0x0C` | `BATTERY` | `GET_BATT_AND_STORAGE` |
| `0x0D` | `DEVICE_INFO` | `DEVICE_QUERY` |
| `0x0E` | `PRIVATE_KEY` | `EXPORT_PRIVATE_KEY` |
| `0x0F` | `DISABLED` | various |
| `0x10` | `CONTACT_MSG_RECV_V3` | `SYNC_NEXT_MESSAGE` (with SNR) |
| `0x11` | `CHANNEL_MSG_RECV_V3` | `SYNC_NEXT_MESSAGE` (with SNR) |
| `0x12` | `CHANNEL_INFO` | `GET_CHANNEL` |
| `0x13` | `SIGN_START` | `SIGN_START` |
| `0x14` | `SIGNATURE` | `SIGN_FINISH` |
| `0x15` | `CUSTOM_VARS` | `GET_CUSTOM_VARS` |
| `0x16` | `ADVERT_PATH` | `GET_ADVERT_PATH` |
| `0x17` | `TUNING_PARAMS` | `GET_TUNING_PARAMS` |
| `0x18` | `STATS` | `GET_STATS` (see §9.6) |
| `0x19` | `AUTOADD_CONFIG` | `GET_AUTOADD_CONFIG` |
| `0x1A` | `ALLOWED_REPEAT_FREQ` | `GET_ALLOWED_REPEAT_FREQ` |
| `0x1C` | `DEFAULT_FLOOD_SCOPE` | `GET_DEFAULT_FLOOD_SCOPE` |

### 9.5 Push Notifications (Unsolicited, type `0x80+`)

Unsolicited frames the firmware can send at any time:

| Code | Name | Surface event in meshcore_py |
|------|------|------------------------------|
| `0x80` | `ADVERTISEMENT` | `ADVERTISEMENT` |
| `0x81` | `PATH_UPDATE` | `PATH_UPDATE` |
| `0x82` | `ACK` | `ACK` |
| `0x83` | `MESSAGES_WAITING` | `MESSAGES_WAITING` |
| `0x84` | `RAW_DATA` | `RAW_DATA` |
| `0x85` | `LOGIN_SUCCESS` | `LOGIN_SUCCESS` |
| `0x86` | `LOGIN_FAILED` | `LOGIN_FAILED` |
| `0x87` | `STATUS_RESPONSE` | `STATUS_RESPONSE` |
| `0x88` | `LOG_DATA` | `LOG_DATA` / `RX_LOG_DATA` |
| `0x89` | `TRACE_DATA` | `TRACE_DATA` |
| `0x8A` | `PUSH_CODE_NEW_ADVERT` | (subset of `ADVERTISEMENT`) |
| `0x8B` | `TELEMETRY_RESPONSE` | `TELEMETRY_RESPONSE` |
| `0x8C` | `BINARY_RESPONSE` | `BINARY_RESPONSE` (and per-type follow-ups) |
| `0x8D` | `PATH_DISCOVERY_RESPONSE` | `PATH_RESPONSE` |
| `0x8E` | `CONTROL_DATA` | `CONTROL_DATA` (incl. `DISCOVER_RESPONSE`) |
| `0x8F` | `CONTACT_DELETED` | `CONTACT_DELETED` |
| `0x90` | `CONTACTS_FULL` | `CONTACTS_FULL` |

`MESSAGES_WAITING` is the trigger to poll `SYNC_NEXT_MESSAGE` until you get
`NO_MORE_MSGS`. `LOG_DATA` carries packet-level debug info — it's the
primary signal for repeat-counting and decoding traffic that didn't
otherwise surface as a parsed message.

### 9.6 Stats Frames (`CMD_GET_STATS`, code 56)

Two-byte command: `[56, sub_type]`.

| Sub-type | Constant | Response size |
|----------|----------|---------------|
| `0` | `STATS_TYPE_CORE` | 11 bytes |
| `1` | `STATS_TYPE_RADIO` | 14 bytes |
| `2` | `STATS_TYPE_PACKETS` | 26 bytes (legacy) or **30 bytes** (with `recv_errors`) |

All stats responses begin with `[0x18, sub_type]` (`RESP_CODE_STATS = 24`).

**`STATS_TYPE_CORE`** (11 bytes):

| Offset | Size | Type | Field |
|--------|------|------|-------|
| 0 | 1 | u8 | response_code (`0x18`) |
| 1 | 1 | u8 | stats_type (`0x00`) |
| 2 | 2 | u16 | battery_mv |
| 4 | 4 | u32 | uptime_secs |
| 8 | 2 | u16 | errors (bitmask) |
| 10 | 1 | u8 | queue_len (TX queue) |

**`STATS_TYPE_RADIO`** (14 bytes):

| Offset | Size | Type | Field |
|--------|------|------|-------|
| 0 | 1 | u8 | response_code (`0x18`) |
| 1 | 1 | u8 | stats_type (`0x01`) |
| 2 | 2 | i16 | noise_floor (dBm) |
| 4 | 1 | i8 | last_rssi (dBm) |
| 5 | 1 | i8 | last_snr (dB×4 — divide by 4 for actual) |
| 6 | 4 | u32 | tx_air_secs |
| 10 | 4 | u32 | rx_air_secs |

**`STATS_TYPE_PACKETS`** (26 or 30 bytes):

| Offset | Size | Type | Field |
|--------|------|------|-------|
| 0 | 1 | u8 | response_code (`0x18`) |
| 1 | 1 | u8 | stats_type (`0x02`) |
| 2 | 4 | u32 | recv (total received) |
| 6 | 4 | u32 | sent (total sent) |
| 10 | 4 | u32 | flood_tx |
| 14 | 4 | u32 | direct_tx |
| 18 | 4 | u32 | flood_rx |
| 22 | 4 | u32 | direct_rx |
| 26 | 4 | u32 | recv_errors (only present in 30-byte frame, firmware ≥ 1.12.0) |

```python
import struct

def parse_stats_packets(frame: bytes) -> dict:
    if len(frame) < 26:
        raise ValueError("packets frame too short")
    code, sub, recv, sent, ftx, dtx, frx, drx = struct.unpack(
        "<B B I I I I I I", frame[:26]
    )
    assert (code, sub) == (24, 2)
    out = dict(
        recv=recv, sent=sent,
        flood_tx=ftx, direct_tx=dtx,
        flood_rx=frx, direct_rx=drx,
    )
    if len(frame) >= 30:
        out["recv_errors"] = struct.unpack("<I", frame[26:30])[0]
    return out
```

Counters are cumulative from boot and may wrap. Invariants:
`recv = flood_rx + direct_rx` and `sent = flood_tx + direct_tx`.

### 9.7 Error Codes

`ERROR` (`0x01`) carries an optional 1-byte code:

| Code | Constant |
|------|----------|
| `0x01` | `ERR_CODE_UNSUPPORTED_CMD` |
| `0x02` | `ERR_CODE_NOT_FOUND` |
| `0x03` | `ERR_CODE_TABLE_FULL` |
| `0x04` | `ERR_CODE_BAD_STATE` |
| `0x05` | `ERR_CODE_FILE_IO_ERROR` |
| `0x06` | `ERR_CODE_ILLEGAL_ARG` |

(Older firmware uses a different set; check `events.py::ErrorMessages` for
the meshcore_py mapping.)

---

## 10. Over-the-Air Request Flows

There are three families of over-the-air requests: anonymous repeater
queries, password-gated logins, and binary requests against an established
DM contact.

### 10.1 Anonymous Repeater Queries (`ANON_REQ` + `req_type`)

Use these when you don't have a DM relationship with a node and want
unauthenticated info from a repeater. From meshcore_py:

```python
from meshcore.packets import AnonReqType

await mc.commands.req_regions_sync(contact)   # AnonReqType.REGIONS = 0x01
await mc.commands.req_owner_sync(contact)     # AnonReqType.OWNER   = 0x02
await mc.commands.req_basic_sync(contact)     # AnonReqType.BASIC   = 0x03 (clock + status)
```

Responses come back as `RESPONSE` packets routed via `reply_path`.
The companion surfaces them as a `BINARY_RESPONSE` plus, where applicable, a
typed event (`STATUS_RESPONSE` for BASIC).

### 10.2 MeshCore Logins — Why CivicMesh Doesn't Use Them

**The Heltec V3 in a CivicMesh node runs companion firmware. Companion
firmware has no login concept at all.** `handleLoginReq` is implemented
only in the **repeater**, **room-server**, and **sensor** firmware variants
(`examples/simple_repeater/MyMesh.cpp:90`,
`examples/simple_room_server/MyMesh.cpp`,
`examples/simple_sensor/SensorMesh.cpp:330`). The companion firmware
(`examples/companion_radio/`) does not instantiate a `ClientACL` and has no
admin password — there is no "log in to a CivicMesh node over LoRa" path,
because there is nothing to log into.

The reverse — a CivicMesh node acting as a *client* logging into someone
else's repeater/room-server via `commands.send_login_sync(contact, pwd)` —
is supported by the protocol and by `meshcore_py`, but is **out of scope
for CivicMesh**. We will not implement either side of MeshCore login.
Mutation against CivicMesh nodes happens over SSH on WiFi/Tailscale.

The rest of this section documents the upstream login mechanism so future
contributors don't have to re-derive it, and so the reasoning behind the
"no logins" stance is explicit.

#### 10.2.1 What login state would look like (on a non-companion node)

If a CivicMesh node ever ran repeater firmware, it would acquire two
persistent files on its onboard filesystem:

- **`/com_prefs`** — the `NodePrefs` struct (`CommonCLI.h`), which adds two
  16-byte plaintext password fields:
  - `password` — admin password (offset 56)
  - `guest_password` — guest password (offset 88)
- **`/s_contacts`** — the `ClientACL` (`ClientACL.cpp`). Up to 20 entries;
  each holds the client's full pubkey, a permissions byte, the learned
  out-path, the ECDH shared secret, and `sync_since`.

Permissions are 2 bits in the lower nibble (`ClientACL.h:7–11`):

| Value | Role |
|-------|------|
| 0 | `PERM_ACL_GUEST` (not persisted in `s_contacts`) |
| 1 | `PERM_ACL_READ_ONLY` |
| 2 | `PERM_ACL_READ_WRITE` |
| 3 | `PERM_ACL_ADMIN` |

When the ACL fills up, new non-guest logins evict the LRU non-admin entry.

#### 10.2.2 Default credentials

The stock admin password is the literal string **`"password"`**, set via
the `ADMIN_PASSWORD` macro at `examples/simple_repeater/MyMesh.cpp:33` and
applied on first boot at line 880. There is no first-boot password reset
flow. Stock-firmware repeaters that have not had their password changed
have, in effect, no admin auth at all.

The guest password defaults empty — a node with an empty `guest_password`
falls through to the "invalid password" branch (`MyMesh.cpp:104`), so
guest logins are off by default.

#### 10.2.3 At rest: plaintext. In transit: encrypted but echoed back.

Passwords are stored plaintext as `char[16]` in `/com_prefs`. No hashing,
no salt, no key derivation (`CommonCLI.cpp:144,152`). Anyone with file
system access reads them directly.

In transit, login is delivered as an `ANON_REQ` (§5.4) — the password is
inside the AES-128-ECB-encrypted payload, with the ECDH shared secret
derived from the sender's pubkey carried in the packet. So a passive
sniffer can't read the password unless they're the destination.

But **the password-change command echoes the new password back over the
air** (`CommonCLI.cpp:288` — comment: *"echo back just to let admin know
for sure!!"*). Whoever issues `password <newpw>` from a logged-in admin
session sees the new value reflected in the reply. Combined with the lack
of any "what's my new password" confirmation step, this means a single
password rotation surfaces the new credential to every node along the
reply path.

Maximum effective password length is **15 characters** (`BaseChatMesh.cpp:557`,
`:561`); the 16th byte is the null terminator. There's no password-policy
enforcement.

#### 10.2.4 No audit log

There is no persistent record of login attempts:

- Successful logins emit `MESH_DEBUG_PRINTLN("Login success!")` to the
  debug serial — only when the firmware is built with `MESH_DEBUG`.
- Failures emit `MESH_DEBUG_PRINTLN("Invalid password: %s", data)` — same
  caveat. Note that this **prints the attempted password to debug
  output**.
- Replay protection exists (`last_timestamp` per client, persisted in
  `s_contacts` for non-guest clients, RAM-only for guests).
- The only persistent, queryable record is the ACL itself: every
  successful non-guest login lazily writes the client's pubkey +
  permissions to `/s_contacts`. **Guest logins leave no trace.** Admins
  can dump the ACL via `req_acl_sync(contact)` — guests and read-only
  clients cannot.

If you want a real audit trail you have to build it yourself: instrument
`handleLoginReq` to write to flash or send a notification DM on each
event.

#### 10.2.5 What bypasses login entirely

Several operations on a repeater/room/sensor are unauthenticated by
design:

| Request | Auth | Notes |
|---------|------|-------|
| `req_basic_sync` (clock + status) | none | rate-limited via `anon_limiter` |
| `req_owner_sync` | none | rate-limited |
| `req_regions_sync` | none | rate-limited |
| `req_status_sync` (`REQ_TYPE_GET_STATUS`) | none | comment in firmware: *"guests can also access this now"* |
| `req_telemetry_sync` (`REQ_TYPE_GET_TELEMETRY_DATA`) | none for base; permission-masked above that | guests get only base telemetry; the request's `perm_mask` byte AND'd with what the server is willing to share governs the rest |
| `req_acl_sync` (`REQ_TYPE_GET_ACCESS_LIST`) | **admin only** (`sender->isAdmin()`) | |
| `req_neighbours_sync` (`REQ_TYPE_GET_NEIGHBOURS`) | login required, role unspecified | check `simple_repeater/MyMesh.cpp:279` for current behavior |
| `req_mma_sync` | depends on sensor firmware | |

The takeaway: even on a "secured" repeater, basic identity, location, and
health information is freely readable by anyone in the mesh. The login
gate is mainly for **mutation** (CLI config changes, ACL edits) and for
**ACL inspection**.

#### 10.2.6 Threat-model summary (and why mutation lives on SSH)

If CivicMesh ran logins on its Heltec, the attack surface would include:

1. **Default password leakage** — any node still on stock `"password"` is
   open. There's no `force_pw_change_on_first_boot` to lean on.
2. **Plaintext at rest** — anyone with shell access to the Pi (or the
   Heltec's filesystem if pulled out of an enclosure) reads admin
   credentials directly.
3. **Echo-on-change** — rotating the password reveals it to every node
   along the reply path, on the air.
4. **No audit** — no way to detect or investigate a compromise except by
   noticing changes in `s_contacts`.
5. **Limited primitive** — you'd still need to bolt on real audit logging
   and a real authn flow application-side.

CivicMesh's existing SSH-on-WiFi/Tailscale path gives us audit (sshd
logs), real key-based auth, key rotation, and tooling we already trust.
The protocol-level login on top of that adds attack surface without
adding capability.

If you ever need to admin a *third-party* repeater you operate
(separately from CivicMesh nodes), do it over a USB-serial console — that
bypasses the LoRa login flow entirely and avoids putting plaintext
credentials on the air.

### 10.3 Binary Requests Against an Established Contact

This is the modern path for status, telemetry, MMA, ACL, and neighbours.
Sent as `BINARY_REQ` (CommandType `50`) and answered as a `BINARY_RESPONSE`
(`0x8C`) push notification:

```python
from meshcore.packets import BinaryReqType
# BinaryReqType: STATUS=1, KEEP_ALIVE=2, TELEMETRY=3, MMA=4, ACL=5, NEIGHBOURS=6
```

The companion correlates request and response via a 4-byte tag returned
when the binary request is dispatched. meshcore_py wraps this for you:

| Helper (in `commands/binary.py`) | Returns | Surface event |
|----------------------------------|---------|---------------|
| `req_status_sync(contact)` | dict (battery, uptime, queue, RSSI, SNR, counters) | `STATUS_RESPONSE` |
| `req_telemetry_sync(contact)` | LPP frame as a list of dicts | `TELEMETRY_RESPONSE` |
| `req_mma_sync(contact, start, end)` | min/max/avg per LPP channel for the time range | `MMA_RESPONSE` |
| `req_acl_sync(contact)` | list of `{key, perm}` | `ACL_RESPONSE` |
| `req_neighbours_sync(contact, ...)` | list of `{pubkey, secs_ago, snr}` | `NEIGHBOURS_RESPONSE` |
| `req_owner_sync(contact)` | owner info string | (typed event) |
| `req_basic_sync(contact)` | clock + brief status | (typed event) |

The dispatched `BINARY_REQ` returns a `MSG_SENT` event with the
`expected_ack` tag and a `suggested_timeout` (in milliseconds × 0.8).
`req_*_sync` then waits on the corresponding event filtered by tag.

#### Status response shape (`req_status_sync`)

Parsed by `parsing.py::parse_status`. Fields, all little-endian:

| Field | Type | Notes |
|-------|------|-------|
| `pubkey_pre` | hex string | 6-byte pubkey prefix |
| `bat` | u16 | millivolts |
| `tx_queue_len` | u16 | |
| `noise_floor` | i16 | dBm |
| `last_rssi` | i16 | dBm |
| `nb_recv` | u32 | |
| `nb_sent` | u32 | |
| `airtime` | u32 | seconds |
| `uptime` | u32 | seconds |
| `sent_flood` | u32 | |
| `sent_direct` | u32 | |
| `recv_flood` | u32 | |
| `recv_direct` | u32 | |
| `full_evts` | u16 | "queue full" events |
| `last_snr` | i16/4 | dB |
| `direct_dups` | u16 | |
| `flood_dups` | u16 | |
| `rx_airtime` | u32 | seconds |
| `recv_errors` | u32 (or `None`) | only on firmware ≥ 1.12.0 |

This is the same data as `STATS_TYPE_*` from §9.6, but pulled over the air
from a remote node rather than the locally connected companion.

### 10.4 Path Discovery and Trace

- `commands.path_discovery_sync(contact)` — sends `PATH_DISCOVERY`
  (CommandType 52) and waits for a `PATH_RESPONSE` (`0x8D`). Useful when a
  direct route has gone stale.
- `commands.send_trace(contact)` — sends a `TRACE` packet (payload type
  `0x09`) that records each hop's SNR×4. The companion surfaces parsed
  results as `TRACE_DATA` (`0x89`).

### 10.5 Airtime Cost (Practical Note)

A binary request is one DM packet out and one back. On SF7/BW62.5 each
direction is roughly 200–400 ms of airtime, plus repeater rebroadcasts on
flood. **Don't poll faster than ~once per minute per remote node** unless
you've measured your channel utilization — over-polling will starve user
text traffic.

---

## 11. Telemetry & Cayenne-LPP

MeshCore telemetry is encoded as Cayenne Low Power Payload (LPP) — a
compact `[channel, type, value]` byte stream where `type` selects a
fixed-width sensor encoding.

### 11.1 Telemetry Mode

A node decides what to publish via three 2-bit fields packed into one byte
(see `commands/device.py::set_other_params`):

```
telemetry_mode_byte:
  bits 0–1 : telemetry_mode_base   (battery, RSSI, SNR, uptime, etc.)
  bits 2–3 : telemetry_mode_loc    (location)
  bits 4–5 : telemetry_mode_env    (environmental: temp, humidity, ...)
  bits 6–7 : reserved
```

For each field, the value selects who can read that category:

- `0` — disabled
- `1` — owner only
- `2` — all logged-in admins
- `3` — public

CivicMesh nodes typically run with `base=3, loc=0, env=0` so anyone in the
mesh can pull battery/health, while location stays private.

### 11.2 Wire Format

A telemetry response is a concatenation of LPP records, each:

```
+---------+------+----------+
| channel | type | value    |
| 1 byte  | 1 B  | varies   |
+---------+------+----------+
```

A trailing `0x00` byte (or end of buffer) ends the frame.

### 11.3 LPP Types Recognized by meshcore_py

From `lpp_json_encoder.py::my_lpp_types`:

| Type | Name | Value shape |
|------|------|-------------|
| 0 | digital input | scalar |
| 1 | digital output | scalar |
| 2 | analog input | scalar |
| 3 | analog output | scalar |
| 100 | generic sensor | scalar |
| 101 | illuminance | scalar |
| 102 | presence | scalar |
| 103 | temperature | scalar |
| 104 | humidity | scalar |
| 113 | accelerometer | `{acc_x, acc_y, acc_z}` |
| 115 | barometer | scalar |
| 116 | voltage | scalar (signed-wrap fixup applied) |
| 117 | current | scalar (signed-wrap fixup applied) |
| 118 | frequency | scalar |
| 120 | percentage | scalar |
| 121 | altitude | scalar |
| 122 | load | scalar |
| 125 | concentration | scalar |
| 128 | power | scalar |
| 130 | distance | scalar |
| 131 | energy | scalar |
| 132 | direction | (library default) |
| 133 | time | scalar |
| 134 | gyrometer | (library default) |
| 135 | colour | `{red, green, blue}` |
| 136 | gps | `{latitude, longitude, altitude}` |
| 142 | switch | scalar |

### 11.4 Decoded Telemetry as Surfaced by meshcore_py

When the companion delivers a `TELEMETRY_RESPONSE` (push code `0x8B`), the
event payload is:

```python
{
    "pubkey_pre": "abcdef012345",       # 6-byte prefix
    "lpp": [
        {"channel": 0, "type": "voltage",     "value": 3.94},
        {"channel": 0, "type": "temperature", "value": 21.5},
        {"channel": 1, "type": "percentage",  "value": 88},
    ],
}
```

Same shape for the binary-request path (`req_telemetry_sync`), with a
different attribute payload (the request `tag` is included for
correlation).

### 11.5 Min/Max/Average (`MMA`)

`req_mma_sync(contact, start, end)` returns aggregates per `(channel,
type)` over the requested time window. Each entry:

```python
{"channel": 0, "type": "temperature", "min": 12.4, "max": 24.1, "avg": 18.6}
```

Supported only on sensor nodes that maintain history.

---

## 12. Admin Operations for CivicMesh

CivicMesh splits the admin surface across two transports:

- **Mutation** (config changes, software updates, restart) goes over **SSH
  on WiFi/Tailscale** to the Pi Zero 2W. Authentication and authorization
  happen at the OS layer; nothing about it is on LoRa.
- **Read-out** (health, telemetry) and **alerts** (low battery, errors,
  custom triggers) flow over **LoRa**: pulled by an admin node, or pushed
  as DMs/channel messages by the CivicMesh node itself.

This section documents only the LoRa side. The SSH side is a normal Linux
admin surface and isn't covered here.

### 12.1 Reading the Local Node (Pi → its own Heltec)

Inside the CivicMesh process running on the Pi, talk to the locally
connected Heltec over USB serial:

```python
from meshcore import MeshCore, EventType

mc = await MeshCore.create_serial("/dev/ttyUSB0", 115200)
# core, radio, packet stats:
core    = await mc.commands.get_stats_core()
radio   = await mc.commands.get_stats_radio()
packets = await mc.commands.get_stats_packets()

# the same data the firmware would return via remote BINARY_REQ STATUS:
local_telem = await mc.commands.get_self_telemetry()
```

`get_self_telemetry` returns a `TELEMETRY_RESPONSE` event whose payload
includes the LPP-decoded list (see §11.4).

### 12.2 Pulling From a Remote CivicMesh Node (admin laptop → CivicMesh node)

From a separate admin node (e.g. a laptop with another Heltec), the
CivicMesh node must already be in your contact list (via QR scan or
`SHARE_CONTACT`). Then:

```python
admin = await MeshCore.create_serial("/dev/ttyUSB1", 115200)
node  = admin.get_contact_by_name("civicmesh-1")

status   = await admin.commands.req_status_sync(node, min_timeout=10)
telemetry = await admin.commands.req_telemetry_sync(node, min_timeout=10)
```

`min_timeout=10` is a sensible floor — the companion suggests a timeout
based on link conditions, but on a long path or busy mesh you want to wait
longer than the suggestion.

The shapes returned are the dicts described in §10.3 and §11.4.

### 12.3 Node-Initiated Alerts (Push)

**MeshCore has no built-in alert primitive.** Alerts in CivicMesh are
application-level: when a condition fires on the node (low battery, link
to internet down, unexpected error count, etc.), the CivicMesh process
sends a regular DM or channel message to a designated admin contact /
channel.

The minimal pattern:

```python
ADMIN_CONTACT_NAME = "ops-laptop"

async def send_alert(mc: MeshCore, severity: str, message: str) -> None:
    contact = mc.get_contact_by_name(ADMIN_CONTACT_NAME)
    if contact is None:
        # fall back to channel so we don't silently swallow alerts
        await mc.commands.send_chan_msg(0, f"[{severity}] {message}")
        return

    result = await mc.commands.send_msg(contact, f"[{severity}] {message}")
    # send_msg returns MSG_SENT; you can wait on ACK if delivery confirmation matters:
    if result.type == EventType.MSG_SENT:
        ack_tag = result.payload["expected_ack"].hex()
        ack = await mc.dispatcher.wait_for_event(
            EventType.ACK,
            attribute_filters={"code": ack_tag},
            timeout=result.payload["suggested_timeout"] / 800,
        )
        # ack is None on no-confirm; don't treat that as a hard failure on a flood-only path
```

**Sizing:** keep alerts short. The protocol-level message limit is 133
characters — split longer alerts into multiple sends with a chunk
indicator (e.g. `[1/2]`).

**Trigger sources** worth wiring up on the Pi side:

- Battery (mV) drops below threshold — read from
  `get_stats_core().battery_mv` on a slow timer.
- TX queue stays non-empty for too long (saturation).
- `recv_errors` (when present) climbs above a baseline.
- Application-side: SSH/WiFi transport down, GPS lost lock, etc.

**Don't alert on** raw `noise_floor` jitter or a single failed ACK — these
are noisy on LoRa and will produce alert fatigue.

### 12.4 Authorization Posture

Mutation goes over SSH (see §10.2 for the rationale on not using MeshCore
logins). The remaining LoRa-side surface is read-only and unauthenticated:

- **Read access over LoRa is open** to anyone in the mesh. Telemetry mode
  governs categories, but anyone with a contact entry can call
  `req_status_sync` and `req_telemetry_sync`. Don't put secrets in custom
  vars or in node names.
- **Push alerts are encrypted but unauthenticated** at the app layer:
  anyone with the channel secret (for channel-broadcast alerts) can spoof
  `[CRITICAL]` messages. For higher-trust alerts, send DMs to a specific
  admin contact — DM origin can at least be tied to the sending pubkey.
- **No on-Heltec admin credentials exist**, by design: the companion
  firmware doesn't implement them, and CivicMesh policy is to keep it
  that way (§10.2).

### 12.5 Subscribing to Inbound Alerts on the Admin Side

On the admin node, alerts arrive as ordinary DMs or channel messages. A
filter handler:

```python
def is_alert(event):
    text = (event.payload or {}).get("text", "")
    return text.startswith("[CRITICAL]") or text.startswith("[WARN]")

def on_dm(event):
    if is_alert(event):
        # forward to ops paging, log, etc.
        record_alert(event.attributes.get("pubkey_prefix"), event.payload["text"])

mc.subscribe(EventType.CONTACT_MSG_RECV, on_dm)
mc.subscribe(EventType.CHANNEL_MSG_RECV, on_dm)
```

If you also want delivery confirmation that a CivicMesh node *acted* on a
config change you pushed via SSH, have the node send a "[ACK] applied
config v17" DM after each successful apply.

---

## 13. Practical Recipes

### 13.1 Detecting Channel Message Repeats

Verify that your channel messages propagate by listening on `RX_LOG_DATA`
and decrypting matching packets:

```python
import asyncio, hashlib, hmac, time
from Crypto.Cipher import AES
from meshcore import MeshCore, EventType

CHANNEL_NAME   = "#civicmesh"
CHANNEL_SECRET = hashlib.sha256(CHANNEL_NAME.encode()).digest()[:16]
CHANNEL_HASH   = hashlib.sha256(CHANNEL_SECRET).digest()[0]

def decrypt_if_ours(payload_hex: str) -> str | None:
    payload = bytes.fromhex(payload_hex)
    header = payload[0]
    if (header >> 2) & 0x0F != 0x05:   # not GRP_TXT
        return None

    # NOTE: path_length packs hop_count + hash_size_code (§4.4)
    path_byte  = payload[1]
    hop_count  = path_byte & 0x3F
    hash_size  = ((path_byte >> 6) & 0x03) + 1
    data_start = 2 + hop_count * hash_size

    if payload[data_start] != CHANNEL_HASH:
        return None

    mac        = payload[data_start + 1 : data_start + 3]
    ciphertext = payload[data_start + 3:]
    if mac != hmac.new(CHANNEL_SECRET, ciphertext, hashlib.sha256).digest()[:2]:
        return None

    plaintext = AES.new(CHANNEL_SECRET, AES.MODE_ECB).decrypt(ciphertext)
    return plaintext[5:].rstrip(b"\x00").decode("utf-8", errors="replace")
```

### 13.2 Sending With Propagation Check

```python
async def send_with_propagation_check(
    mc: MeshCore, channel_idx: int, text: str, timeout: float = 5.0
) -> dict:
    repeats = []

    def on_rx(event):
        msg = decrypt_if_ours(event.payload.get("payload", ""))
        if msg and text in msg:
            repeats.append({"snr": event.payload.get("snr"),
                            "rssi": event.payload.get("rssi")})

    sub = mc.subscribe(EventType.RX_LOG_DATA, on_rx)
    try:
        result = await mc.commands.send_chan_msg(channel_idx, text)
        if result.type == EventType.ERROR:
            return {"status": "send_failed", "repeats": 0}
        await asyncio.sleep(timeout)
        return {
            "status":  "propagated" if repeats else "no_propagation",
            "repeats": len(repeats),
            "details": repeats,
        }
    finally:
        sub.unsubscribe()
```

### 13.3 Remote Telemetry Pull (Admin → CivicMesh Node)

```python
async def poll_node(admin: MeshCore, node_name: str) -> dict | None:
    contact = admin.get_contact_by_name(node_name)
    if contact is None:
        return None

    status    = await admin.commands.req_status_sync(contact, min_timeout=10)
    telemetry = await admin.commands.req_telemetry_sync(contact, min_timeout=10)

    return {"status": status, "telemetry": telemetry}
```

A reasonable poll cadence per remote node is ~60 s; faster than that and
you're meaningfully cutting into shared airtime.

### 13.4 Low-Battery Alert (Pi Side)

```python
LOW_BATTERY_MV = 3500
ALERT_INTERVAL = 60 * 60   # don't spam: at most once an hour

last_alert = 0.0

async def battery_watch(mc: MeshCore) -> None:
    global last_alert
    while True:
        core = await mc.commands.get_stats_core()
        if core.type == EventType.STATS_CORE:
            mv = core.payload.get("battery_mv", 0)
            now = time.monotonic()
            if mv and mv < LOW_BATTERY_MV and now - last_alert > ALERT_INTERVAL:
                await send_alert(mc, "WARN", f"battery {mv}mV")
                last_alert = now
        await asyncio.sleep(60)
```

### 13.5 ACK-Confirmed Alert

```python
async def send_alert_with_ack(mc, contact, text, timeout_s: float = 30.0) -> bool:
    result = await mc.commands.send_msg(contact, text)
    if result.type != EventType.MSG_SENT:
        return False
    ack_tag = result.payload["expected_ack"].hex()
    ack = await mc.dispatcher.wait_for_event(
        EventType.ACK,
        attribute_filters={"code": ack_tag},
        timeout=timeout_s,
    )
    return ack is not None
```

CLI commands (`txt_type=0x01`) do not produce ACKs — only plain text DMs
do. Don't wait for an ACK on a CLI command; it will always time out.

---

## 14. QR Codes

For sharing channels and contacts between nodes/apps. Authoritative
reference: `MeshCore/docs/qr_codes.md`.

### 14.1 Channel

```
meshcore://channel/add?name=Public&secret=8b3387e9c5cdea6ac9e5edbaa115cd72
```

- `name`: channel name, URL-encoded if needed.
- `secret`: 16 raw bytes as 32 hex characters.

### 14.2 Contact

```
meshcore://contact/add?name=Example+Contact&public_key=<64 hex chars>&type=1
```

- `name`: URL-encoded contact name.
- `public_key`: 32 raw bytes as 64 hex characters.
- `type`: contact type — `1` companion, `2` repeater, `3` room server,
  `4` sensor.

---

## 15. Hardware Notes

### 15.1 Recommended Hardware

| Use Case | Hardware | Notes |
|----------|----------|-------|
| Companion | Heltec V3 | Good BLE, USB-C, ESP32 |
| Repeater (solar) | RAK4631 | Low-power nRF52 |
| Repeater (powered) | Heltec V3 | More headroom for features |
| Standalone client | T-Deck | Built-in keyboard/screen |
| CivicMesh node | Heltec V3 + Pi Zero 2W | Heltec for radio, Pi for compute and SSH |

### 15.2 Common Issues

| Symptom | Likely cause |
|---------|--------------|
| Can't flash | Charge-only USB cable (no data lines) |
| No reception | Antenna connector loose or no antenna |
| Settings appear to revert | Reboot required after some param changes |
| Boot loop on battery | Cell can't sustain TX-spike current |
| BLE keeps disconnecting | Heltec V3 BLE stack quirks; prefer USB serial when possible |

### 15.3 Power

- USB power banks may auto-shut-off on low draw — use ones with an
  "always-on" / IoT mode (Voltaic Systems batteries advertise this).
- LiFePO4 is preferable for outdoor enclosures: wider temp range, safer
  chemistry.
- Never charge LiPo below 0 °C.

### 15.4 Flashing

ESP32 bootloader sequence: hold `BOOT` → press `RST` → release `BOOT`.

Web tools:

- Flasher: <https://flasher.meshcore.co.uk>
- Config: <https://config.meshcore.dev>

---

## 16. Resources & Quick Reference

### 16.1 Official

- GitHub: <https://github.com/meshcore-dev/MeshCore>
- Web flasher: <https://flasher.meshcore.co.uk>
- Web config: <https://config.meshcore.dev>

### 16.2 Community

- Puget Mesh: <https://pugetmesh.org/meshcore/>
- Python library on PyPI: <https://pypi.org/project/meshcore/>

### 16.3 Related Projects

| Project | Description |
|---------|-------------|
| `meshcore_py` | Official Python library |
| `meshcore.js` | JavaScript/TypeScript library |
| `meshcoremqtt` | MQTT bridge |
| MeshCore for Home Assistant | HA integration |

### 16.4 Header-Byte Cheatsheet

```
0x11 = ADVERT, FLOOD
0x15 = GRP_TXT, FLOOD          (channel message)
0x19 = GRP_DATA, FLOOD
0x09 = TXT_MSG, FLOOD          (DM, first send)
0x0A = TXT_MSG, DIRECT         (DM, established route)
0x0D = ACK, FLOOD
0x0E = ACK, DIRECT
0x1D = ANON_REQ, FLOOD
```

### 16.5 Channel Hash Derivation

```python
secret    = sha256(channel_name)[:16]
hash_byte = sha256(secret)[0]   # NOT sha256(channel_name)[0]
```

### 16.6 path_length Decoding

```python
hop_count = b & 0x3F
hash_size = ((b >> 6) & 0x03) + 1   # 1, 2, or 3 bytes
```

### 16.7 Channel Message Layout

```
Outer payload:
+--------------+-----+----------------------------------------+
| channel_hash | MAC | AES-128-ECB(secret, plaintext)         |
| 1 byte       | 2 B | padded to 16-byte boundary             |
+--------------+-----+----------------------------------------+

Plaintext:
+-----------+-------------------+----------------------+
| timestamp | txt_type+attempt  | "<sender>: <text>"   |
| 4 bytes   | 1 byte (6+2 bits) | UTF-8                |
+-----------+-------------------+----------------------+
```

---

*Maintained for the CivicMesh project. Last full audit: 2026-05.*
