# DAVE MLS — Discord E2E Encryption

## What is DAVE?

DAVE (Discord Audio and Video Encryption) is Discord's end-to-end encryption layer for voice and video, based on the MLS (Messaging Layer Security) protocol. It was rolled out to all servers starting in late 2024.

When DAVE is active, audio frames are encrypted at the client before being sent over the network. The Discord server cannot decrypt them — only participants who have completed the MLS key exchange can.

py-cord 2.7.x declares DAVE v1 support in the WebSocket handshake but does not implement the MLS key exchange or frame decryption. Without this implementation, the bot receives only encrypted frames and records silence.

---

## Implementation overview

The DAVE implementation lives in two files:

- **`dave_handler.py`** — `DaveHandler` class: MLS state machine and per-frame decryption
- **`bot.py`** — three monkey-patches to py-cord's WebSocket and decode pipeline

### Monkey-patches applied at import time

| Patched method | Purpose |
|---|---|
| `DiscordVoiceWebSocket.identify` | Add `max_dave_protocol_version: 1` to IDENTIFY payload |
| `DiscordVoiceWebSocket.select_protocol` | Add `dave_protocol_version: 1` to SELECT_PROTOCOL payload |
| `DiscordVoiceWebSocket.received_message` | Forward JSON DAVE opcodes (20, 21, 22, 24) to `DaveHandler` |
| `DiscordVoiceWebSocket.poll_event` | Intercept binary WebSocket frames (py-cord drops them silently) |
| `DecodeManager.run` | DAVE-decrypt each audio frame before Opus decoding |

All patches preserve the original method via `_orig` attribute and use `_unwrap()` to follow the chain — safe for `importlib.reload()`.

---

## MLS handshake sequence

```
Bot                    Discord server
 │                           │
 │──── IDENTIFY (max_dave=1) ──►│
 │◄─── SESSION_DESCRIPTION ─────│  (confirms DAVE negotiation)
 │                           │
 │◄─── OP 20 (binary) ──────────│  PREPARE_EPOCH  epoch=1
 │     → DaveHandler.handle_json()
 │     → reset_session() if epoch=1
 │     → get_serialized_key_package()
 │──── OP 26 (binary) ──────────►│  MLS_KEY_PACKAGE
 │                           │
 │◄─── OP 25 (binary) ──────────│  MLS_EXTERNAL_SENDER
 │     → session.set_external_sender()
 │     → get_serialized_key_package()
 │──── OP 26 (binary) ──────────►│  MLS_KEY_PACKAGE
 │                           │
 │◄─── OP 27 (binary) ──────────│  MLS_PROPOSALS  (add/remove members)
 │     → session.process_proposals()
 │──── OP 28 (binary) ──────────►│  MLS_COMMIT_WELCOME
 │                           │
 │◄─── OP 29 (binary) ──────────│  MLS_ANNOUNCE_COMMIT  tid=N
 │     → session.process_commit()
 │──── OP 23 (JSON)  ───────────►│  DAVE_TRANSITION_READY  tid=N
 │                           │
 │  session.ready = True         │
 │  Audio frames now decryptable │
 │                           │
 │◄─── OP 30 (binary) ──────────│  MLS_WELCOME  (for late joiners)
 │     → session.process_welcome()
 │──── OP 23 (JSON)  ───────────►│  DAVE_TRANSITION_READY
```

After `session.ready` is True, every incoming audio frame is passed through `DaveHandler.decrypt()` before Opus decoding.

---

## Binary frame format

**Incoming** (server → bot):
```
[2 bytes: seq, big-endian] [1 byte: op] [N bytes: payload]
```

**Outgoing** (bot → server):
```
[1 byte: op] [N bytes: data]
```

No sequence prefix on outgoing frames.

---

## Opcode reference

### Binary opcodes

| Op | Direction | Name | Description |
|---|---|---|---|
| 25 | Server → Bot | MLS_EXTERNAL_SENDER | External sender package for the MLS group |
| 26 | Bot → Server | MLS_KEY_PACKAGE | Bot's MLS key package |
| 27 | Server → Bot | MLS_PROPOSALS | Add/remove member proposals |
| 28 | Bot → Server | MLS_COMMIT_WELCOME | Commit + optional welcome bytes |
| 29 | Server → Bot | MLS_ANNOUNCE_COMMIT | Commit data + transition ID |
| 30 | Server → Bot | MLS_WELCOME | Welcome message for new members |

### JSON opcodes

| Op | Direction | Name | Description |
|---|---|---|---|
| 20 | Server → Bot | DAVE_PREPARE_EPOCH | Triggers session init (Discord uses 20, docs say 24) |
| 23 | Bot → Server | DAVE_TRANSITION_READY | Acknowledges a committed transition |

---

## Audio decryption

Each decoded audio packet goes through:

```python
# DecodeManager.run (patched)
raw = data.decrypted_data          # RTP-decrypted payload (still DAVE-encrypted)

if dave.ready and not dave.can_passthrough(user_id):
    opus_bytes = dave.decrypt(user_id, raw)   # DAVE decrypt → plain Opus
    if opus_bytes is None:
        continue                               # drop frame
elif dave.ready and dave.can_passthrough(user_id):
    opus_bytes = dave.decrypt(user_id, raw) or raw  # try DAVE, fall back to raw

decoded_pcm = opus_decoder.decode(opus_bytes)
```

`can_passthrough()` returns True during the MLS transition window when some users have not yet completed the key exchange. During this window unencrypted frames from those users are acceptable.

---

## MLS library: davey

The actual MLS cryptography is handled by `davey`, a Python C extension that wraps Discord's own MLS implementation.

```python
from davey import DaveSession, MediaType, ProposalsOperationType, DAVE_PROTOCOL_VERSION

session = DaveSession(DAVE_PROTOCOL_VERSION, user_id, channel_id)
session.set_external_sender(payload)
kp = session.get_serialized_key_package()
result = session.process_proposals(ProposalsOperationType.append, proposals)
session.process_commit(commit_data)
session.process_welcome(welcome_data)

# session.ready → bool
# session.epoch → int
# session.status → str

plain_opus = session.decrypt(user_id, MediaType.audio, encrypted_frame)
```

---

## Reconnection handling

When the bot reconnects to a voice channel:
- `importlib.reload(bot)` is called from the GUI, which re-applies all patches safely via `_unwrap()`
- A fresh `DaveHandler` is created before `channel.connect()` and stored in `_pending_dave_handler`
- The pending handler is auto-attached to the WebSocket the moment the first DAVE message arrives

On `PREPARE_EPOCH` with `epoch=1`, the handler calls `session.reinit()` to restart the MLS group without creating a new Python object.
