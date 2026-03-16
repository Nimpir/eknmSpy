"""
dave_handler.py — DAVE MLS key exchange and frame decryption for Discord voice.

Discord sends DAVE MLS messages as BINARY WebSocket frames (not JSON).
Binary frame format (incoming): [seq: 2B big-endian][op: 1B][payload...]

Incoming opcodes (binary):
  25  MLS_EXTERNAL_SENDER   server → us: external sender package
  27  MLS_PROPOSALS         server → us: add/remove proposals
  29  MLS_ANNOUNCE_COMMIT   server → us: commit announcement with transition_id
  30  MLS_WELCOME           server → us: MLS welcome with transition_id

Outgoing opcodes (binary):
  26  MLS_KEY_PACKAGE       us → server: [1B op][key_package_bytes]
  28  MLS_COMMIT_WELCOME    us → server: [1B op][commit_bytes][welcome_bytes?]

Outgoing opcodes (JSON):
  23  DAVE_TRANSITION_READY us → server: {"op": 23, "d": {"transition_id": N}}

Incoming JSON opcodes (handled in _patched_received_message in bot.py):
  21  DAVE_PREPARE_TRANSITION  server → us (JSON)
  22  DAVE_EXECUTE_TRANSITION  server → us (JSON)
  24  DAVE_PREPARE_EPOCH       server → us (JSON) — epoch=1 triggers session reinit
"""

import struct
import logging
from typing import Optional

from davey import DaveSession, MediaType, ProposalsOperationType, DAVE_PROTOCOL_VERSION

log = logging.getLogger("dave_handler")

# Incoming binary opcodes
OP_EXTERNAL_SENDER  = 25
OP_PROPOSALS        = 27
OP_ANNOUNCE_COMMIT  = 29
OP_WELCOME          = 30

# Outgoing binary opcodes
OP_KEY_PACKAGE      = 26
OP_COMMIT_WELCOME   = 28

# Outgoing JSON opcode
OP_TRANSITION_READY = 23

# JSON opcodes we need to handle in received_message
OP_PREPARE_EPOCH    = 24


class DaveHandler:
    """
    Manages the DAVE MLS lifecycle for one voice channel connection.

    Attaches to a py-cord DiscordVoiceWebSocket. Receives binary DAVE frames
    via handle_binary(), performs the MLS key exchange, and exposes decrypt()
    so the audio pipeline can unwrap DAVE-encrypted audio frames.
    """

    def __init__(self, user_id: int, channel_id: int):
        self._user_id    = user_id
        self._channel_id = channel_id
        self._session: Optional[DaveSession] = None
        self._ws = None
        self._passthrough_state: dict[int, bool] = {}  # user_id → last passthrough value

    # ── Lifecycle ──────────────────────────────────────────────────────────────

    def attach(self, ws) -> None:
        self._ws = ws
        ws._dave = self
        log.debug("DAVE handler attached to ws for channel %s", self._channel_id)

    def detach(self) -> None:
        if self._ws is not None and hasattr(self._ws, "_dave"):
            del self._ws._dave
        self._session = None
        self._ws      = None
        log.debug("DAVE handler detached")

    # ── Properties ─────────────────────────────────────────────────────────────

    @property
    def ready(self) -> bool:
        return self._session is not None and self._session.ready

    @property
    def status(self):
        return self._session.status if self._session else None

    # ── Internal ───────────────────────────────────────────────────────────────

    def _get_or_create_session(self) -> DaveSession:
        if self._session is None:
            self._session = DaveSession(
                DAVE_PROTOCOL_VERSION, self._user_id, self._channel_id
            )
            log.info("DAVE session created v%s user=%s channel=%s",
                     DAVE_PROTOCOL_VERSION, self._user_id, self._channel_id)
        return self._session

    def reset_session(self) -> None:
        """Reinitialize MLS session (called on PREPARE_EPOCH with epoch=1)."""
        if self._session is not None:
            try:
                self._session.reinit(DAVE_PROTOCOL_VERSION, self._user_id, self._channel_id)
                log.info("DAVE session reinitialized")
            except Exception:
                log.exception("DAVE session.reinit failed; creating fresh session")
                self._session = None
        # If reinit failed or session didn't exist, create fresh on next use

    # ── Binary send helpers ─────────────────────────────────────────────────────

    async def _send_binary(self, ws, op: int, data: bytes) -> None:
        """Send binary DAVE frame: [1B op][data...]  (no seq prefix on outgoing)."""
        try:
            await ws.ws.send_bytes(bytes([op]) + data)
            log.debug("DAVE sent binary op=%d len=%d", op, len(data))
        except Exception:
            log.exception("DAVE failed to send binary op=%d", op)

    async def _send_transition_ready(self, ws, transition_id: int) -> None:
        """Send DAVE_TRANSITION_READY (op 23) as JSON."""
        try:
            await ws.send_as_json({
                "op": OP_TRANSITION_READY,
                "d": {"transition_id": transition_id},
            })
            log.debug("DAVE sent TRANSITION_READY tid=%d", transition_id)
        except Exception:
            log.exception("DAVE failed to send TRANSITION_READY tid=%d", transition_id)

    # ── Binary message dispatch ─────────────────────────────────────────────────

    async def handle_binary(self, ws, data: bytes) -> None:
        """Dispatch an incoming binary voice WebSocket frame."""
        if len(data) < 3:
            log.warning("DAVE binary frame too short: %d bytes", len(data))
            return

        # Incoming format: [2B seq big-endian][1B op][payload...]
        seq     = struct.unpack_from(">H", data, 0)[0]
        op      = data[2]
        payload = data[3:]

        log.debug("DAVE binary op=%d seq=%d payload=%d bytes", op, seq, len(payload))

        if op == OP_EXTERNAL_SENDER:
            await self._on_external_sender(ws, payload)
        elif op == OP_PROPOSALS:
            await self._on_proposals(ws, payload)
        elif op == OP_ANNOUNCE_COMMIT:
            await self._on_announce_commit(ws, payload)
        elif op == OP_WELCOME:
            await self._on_welcome(ws, payload)
        else:
            log.debug("DAVE unknown binary op=%d", op)

    # ── JSON message handling ───────────────────────────────────────────────────

    async def handle_json(self, ws, msg: dict) -> None:
        """Handle JSON-level DAVE opcodes (20, 21, 22, 24) from received_message."""
        op   = msg.get("op")
        data = msg.get("d") or {}
        log.debug("handle_json op=%s d=%s", op, data)

        # Discord production sends op 20 as DAVE_PREPARE_EPOCH (documented as 24)
        if op in (OP_PREPARE_EPOCH, 20) and "epoch" in data:
            epoch   = data.get("epoch", 0)
            version = data.get("protocol_version", DAVE_PROTOCOL_VERSION)
            log.info("DAVE PREPARE_EPOCH (op=%s) epoch=%d version=%d", op, epoch, version)
            if epoch == 1:
                # New MLS group being prepared — reinitialise our session
                self.reset_session()
                session = self._get_or_create_session()
                try:
                    kp = session.get_serialized_key_package()
                    await self._send_binary(ws, OP_KEY_PACKAGE, kp)
                    log.info("DAVE OP26 key package sent after PREPARE_EPOCH (%d bytes)", len(kp))
                except Exception:
                    log.exception("DAVE OP26 failed after PREPARE_EPOCH")

    # ── OP 25 — external sender ─────────────────────────────────────────────────

    async def _on_external_sender(self, ws, payload: bytes) -> None:
        session = self._get_or_create_session()
        try:
            session.set_external_sender(payload)
            log.info("DAVE OP25: external sender set (%d bytes)", len(payload))
        except Exception:
            log.exception("DAVE OP25: set_external_sender failed")
            return

        try:
            kp = session.get_serialized_key_package()
            await self._send_binary(ws, OP_KEY_PACKAGE, kp)
            log.info("DAVE OP26: key package sent (%d bytes)", len(kp))
        except Exception:
            log.exception("DAVE OP26: failed to send key package")

    # ── OP 27 — proposals ───────────────────────────────────────────────────────

    async def _on_proposals(self, ws, payload: bytes) -> None:
        if not payload:
            log.warning("DAVE OP27: empty payload")
            return

        session    = self._get_or_create_session()
        optype_val = payload[0]
        proposals  = payload[1:]

        op_type = (ProposalsOperationType.append
                   if optype_val == 0 else ProposalsOperationType.revoke)
        log.info("DAVE OP27: proposals op=%s proposals_len=%d", op_type, len(proposals))

        try:
            result = session.process_proposals(op_type, proposals)
        except Exception:
            log.exception("DAVE OP27: process_proposals failed")
            return

        if result is not None:
            # We are an existing member — send commit (+ optional welcome) back
            commit_data = result.commit + (result.welcome if result.welcome else b"")
            await self._send_binary(ws, OP_COMMIT_WELCOME, commit_data)
            log.info("DAVE OP28: commit sent (%d bytes), has_welcome=%s, status=%s",
                     len(result.commit), result.welcome is not None, session.status)

    # ── OP 29 — announce commit / transition ───────────────────────────────────

    async def _on_announce_commit(self, ws, payload: bytes) -> None:
        if len(payload) < 2:
            log.warning("DAVE OP29: payload too short (%d bytes)", len(payload))
            return

        transition_id = struct.unpack_from(">H", payload, 0)[0]
        commit_data   = payload[2:]
        session       = self._get_or_create_session()

        if commit_data:
            try:
                session.process_commit(commit_data)
                log.info("DAVE OP29: commit processed tid=%d status=%s epoch=%s",
                         transition_id, session.status, session.epoch)
            except Exception:
                log.exception("DAVE OP29: process_commit failed tid=%d", transition_id)

        if transition_id != 0:
            await self._send_transition_ready(ws, transition_id)

    # ── OP 30 — welcome ─────────────────────────────────────────────────────────

    async def _on_welcome(self, ws, payload: bytes) -> None:
        if len(payload) < 2:
            log.warning("DAVE OP30: payload too short (%d bytes)", len(payload))
            return

        transition_id = struct.unpack_from(">H", payload, 0)[0]
        welcome_data  = payload[2:]
        session       = self._get_or_create_session()

        try:
            session.process_welcome(welcome_data)
            log.info("DAVE OP30: welcome processed tid=%d ready=%s epoch=%s status=%s",
                     transition_id, session.ready, session.epoch, session.status)
        except Exception:
            log.exception("DAVE OP30: process_welcome failed tid=%d", transition_id)
            return

        if transition_id != 0:
            await self._send_transition_ready(ws, transition_id)

    # ── Audio decryption ────────────────────────────────────────────────────────

    def decrypt(self, user_id: int, frame: bytes) -> Optional[bytes]:
        """
        DAVE-decrypt one audio frame.
        Returns decrypted Opus bytes, or None if not ready / decryption fails.
        """
        if self._session is None or not self._session.ready:
            return None
        try:
            return self._session.decrypt(user_id, MediaType.audio, frame)
        except Exception as e:
            log.debug("DAVE decrypt user=%s: %s", user_id, e)
            return None

    def can_passthrough(self, user_id: int) -> bool:
        """True if unencrypted frames are acceptable for this user (transition period)."""
        if self._session is None:
            return True
        try:
            result = self._session.can_passthrough(user_id)
        except Exception:
            result = True
        prev = self._passthrough_state.get(user_id)
        if prev is None or prev != result:
            self._passthrough_state[user_id] = result
            log.info("DAVE passthrough changed: user=%s passthrough=%s", user_id, result)
        return result
